import sys
sys.path.append('thirdparty/AdaptiveWingLoss')
import os, glob
import numpy as np
import cv2
import argparse
from src.approaches.train_image_translation import Image_translation_block
import torch
import pickle
import face_alignment
from src.autovc.AutoVC_mel_Convertor_retrain_version import AutoVC_mel_Convertor
import shutil
import util.utils as util
from scipy.signal import savgol_filter

from src.approaches.train_audio2landmark import Audio2landmark_model

default_head_name = 'dali'
ADD_NAIVE_EYE = True
CLOSE_INPUT_FACE_MOUTH = False

parser = argparse.ArgumentParser()
parser.add_argument('--jpg', type=str, required=True, help='Path to input jpg image')
parser.add_argument('--wav', type=str, required=True, help='Path to input wav audio file')
parser.add_argument('--close_input_face_mouth', default=CLOSE_INPUT_FACE_MOUTH, action='store_true')

parser.add_argument('--load_AUTOVC_name', type=str, default='examples/ckpt/ckpt_autovc.pth')
parser.add_argument('--load_a2l_G_name', type=str, default='examples/ckpt/ckpt_speaker_branch.pth')
parser.add_argument('--load_a2l_C_name', type=str, default='examples/ckpt/ckpt_content_branch.pth')
parser.add_argument('--load_G_name', type=str, default='examples/ckpt/ckpt_116_i2i_comb.pth')

parser.add_argument('--amp_lip_x', type=float, default=2.)
parser.add_argument('--amp_lip_y', type=float, default=2.)
parser.add_argument('--amp_pos', type=float, default=.5)
parser.add_argument('--reuse_train_emb_list', type=str, nargs='+', default=[])
parser.add_argument('--add_audio_in', default=False, action='store_true')
parser.add_argument('--comb_fan_awing', default=False, action='store_true')
parser.add_argument('--output_folder', type=str, default='examples')

parser.add_argument('--test_end2end', default=True, action='store_true')
parser.add_argument('--dump_dir', type=str, default='', help='')
parser.add_argument('--pos_dim', default=7, type=int)
parser.add_argument('--use_prior_net', default=True, action='store_true')
parser.add_argument('--transformer_d_model', default=32, type=int)
parser.add_argument('--transformer_N', default=2, type=int)
parser.add_argument('--transformer_heads', default=2, type=int)
parser.add_argument('--spk_emb_enc_size', default=16, type=int)
parser.add_argument('--init_content_encoder', type=str, default='')
parser.add_argument('--lr', type=float, default=1e-3, help='learning rate')
parser.add_argument('--reg_lr', type=float, default=1e-6, help='weight decay')
parser.add_argument('--write', default=False, action='store_true')
parser.add_argument('--segment_batch_size', type=int, default=1, help='batch size')
parser.add_argument('--emb_coef', default=3.0, type=float)
parser.add_argument('--lambda_laplacian_smooth_loss', default=1.0, type=float)
parser.add_argument('--use_11spk_only', default=False, action='store_true')

opt_parser = parser.parse_args()

print(list(face_alignment.LandmarksType))

''' STEP 1: preprocess input single image '''
image_path = opt_parser.jpg
if not os.path.exists(image_path):
    print(f"Error: Image file {image_path} not found.")
    exit(-1)
img = cv2.imread(image_path)
if img is None:
    print(f"Error: Unable to load image from {image_path}.")
    exit(-1)
else:
    print(f"Image loaded successfully from {image_path}.")

predictor = face_alignment.FaceAlignment(face_alignment.LandmarksType.THREE_D, device='cuda', flip_input=True)
shapes = predictor.get_landmarks_from_image(img)
if (not shapes or len(shapes) != 1):
    print('Cannot detect face landmarks. Exit.')
    exit(-1)
shape_3d = shapes[0]

if opt_parser.close_input_face_mouth:
    util.close_input_face_mouth(shape_3d)

''' Additional manual adjustment to input face landmarks (slimmer lips and wider eyes) '''
shape_3d[49:54, 1] += 1.
shape_3d[55:60, 1] -= 1.
shape_3d[[37, 38, 43, 44], 1] -= 2
shape_3d[[40, 41, 46, 47], 1] += 2

''' STEP 2: normalize face as input to audio branch '''
shape_3d, scale, shift = util.norm_input_face(shape_3d)

''' STEP 3: Generate audio data as input to audio branch '''
# audio real data
au_data = []
au_emb = []
ains = [opt_parser.wav]
for ain in ains:
    # 生成音频嵌入
    from thirdparty.resemblyer_util.speaker_emb import get_spk_emb
    me, ae = get_spk_emb(ain)
    au_emb.append(me.reshape(-1))

    print('Processing audio file', ain)
    c = AutoVC_mel_Convertor('examples')

    au_data_i = c.convert_single_wav_to_autovc_input(audio_filename=ain, autovc_model_path=opt_parser.load_AUTOVC_name)
    au_data += au_data_i

# landmark fake placeholder
fl_data = []
rot_tran, rot_quat, anchor_t_shape = [], [], []
for au, info in au_data:
    au_length = au.shape[0]
    fl = np.zeros(shape=(au_length, 68 * 3))
    fl_data.append((fl, info))
    rot_tran.append(np.zeros(shape=(au_length, 3, 4)))
    rot_quat.append(np.zeros(shape=(au_length, 4)))
    anchor_t_shape.append(np.zeros(shape=(au_length, 68 * 3)))

if os.path.exists(os.path.join('examples', 'dump', 'random_val_fl.pickle')):
    os.remove(os.path.join('examples', 'dump', 'random_val_fl.pickle'))
if os.path.exists(os.path.join('examples', 'dump', 'random_val_fl_interp.pickle')):
    os.remove(os.path.join('examples', 'dump', 'random_val_fl_interp.pickle'))
if os.path.exists(os.path.join('examples', 'dump', 'random_val_au.pickle')):
    os.remove(os.path.join('examples', 'dump', 'random_val_au.pickle'))
if os.path.exists(os.path.join('examples', 'dump', 'random_val_gaze.pickle')):
    os.remove(os.path.join('examples', 'dump', 'random_val_gaze.pickle'))

with open(os.path.join('examples', 'dump', 'random_val_fl.pickle'), 'wb') as fp:
    pickle.dump(fl_data, fp)
with open(os.path.join('examples', 'dump', 'random_val_au.pickle'), 'wb') as fp:
    pickle.dump(au_data, fp)
with open(os.path.join('examples', 'dump', 'random_val_gaze.pickle'), 'wb') as fp:
    gaze = {'rot_trans': rot_tran, 'rot_quat': rot_quat, 'anchor_t_shape': anchor_t_shape}
    pickle.dump(gaze, fp)

''' STEP 4: RUN audio->landmark network '''
model = Audio2landmark_model(opt_parser, jpg_shape=shape_3d)
if len(opt_parser.reuse_train_emb_list) == 0:
    model.test(au_emb=au_emb)
else:
    model.test(au_emb=None)

''' STEP 5: de-normalize the output to the original image scale '''
fls = glob.glob1('examples', 'pred_fls_*.txt')
fls.sort()

for i in range(0, len(fls)):
    fl = np.loadtxt(os.path.join('examples', fls[i])).reshape((-1, 68, 3))
    fl[:, :, 0:2] = -fl[:, :, 0:2]
    fl[:, :, 0:2] = fl[:, :, 0:2] / scale - shift

    if ADD_NAIVE_EYE:
        fl = util.add_naive_eye(fl)

    # additional smooth
    fl = fl.reshape((-1, 204))
    fl[:, :48 * 3] = savgol_filter(fl[:, :48 * 3], 15, 3, axis=0)
    fl[:, 48 * 3:] = savgol_filter(fl[:, 48 * 3:], 5, 3, axis=0)
    fl = fl.reshape((-1, 68, 3))

    ''' STEP 6: Imag2image translation '''
    model = Image_translation_block(opt_parser, single_test=True)
    with torch.no_grad():
        model.single_test(jpg=img, fls=fl, filename=fls[i], prefix=os.path.splitext(os.path.basename(opt_parser.jpg))[0])
        print('finish image2image gen')
    os.remove(os.path.join('examples', fls[i]))