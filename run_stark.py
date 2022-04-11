import os
import sys
import argparse
import cv2
from easydict import EasyDict as edict
from PIL import Image
import yaml
import numpy as np
from collections import OrderedDict
import time
import math
import torch
from lib.utils.misc import NestedTensor
from copy import deepcopy

prj_dir = r'C:\Users\zadorozhnyy.v\Downloads\mystark'

data_dir = r'C:\Users\zadorozhnyy.v\Downloads\Stark-main\data\got10k\test'


def merge_template_search(inp_list, return_search=False, return_template=False):
    """NOTICE: search region related features must be in the last place"""
    seq_dict = {"feat": torch.cat([x["feat"] for x in inp_list], dim=0),
                "mask": torch.cat([x["mask"] for x in inp_list], dim=1),
                "pos": torch.cat([x["pos"] for x in inp_list], dim=0)}
    if return_search:
        x = inp_list[-1]
        seq_dict.update({"feat_x": x["feat"], "mask_x": x["mask"], "pos_x": x["pos"]})
    if return_template:
        z = inp_list[0]
        seq_dict.update({"feat_z": z["feat"], "mask_z": z["mask"], "pos_z": z["pos"]})
    return seq_dict

def sample_target(im, target_bb, search_area_factor, output_sz=None):
    """ Extracts a square crop centered at target_bb box, of area search_area_factor^2 times target_bb area

    args:
        im - cv image
        target_bb - target box [x, y, w, h]
        search_area_factor - Ratio of crop size to target size
        output_sz - (float) Size to which the extracted crop is resized (always square). If None, no resizing is done.

    returns:
        cv image - extracted crop
        float - the factor by which the crop has been resized to make the crop size equal output_size
    """
    if not isinstance(target_bb, list):
        x, y, w, h = target_bb.tolist()
    else:
        x, y, w, h = target_bb
    # Crop image
    crop_sz = math.ceil(math.sqrt(w * h) * search_area_factor)

    if crop_sz < 1:
        raise Exception('Too small bounding box.')

    x1 = round(x + 0.5 * w - crop_sz * 0.5)
    x2 = x1 + crop_sz

    y1 = round(y + 0.5 * h - crop_sz * 0.5)
    y2 = y1 + crop_sz

    x1_pad = max(0, -x1)
    x2_pad = max(x2 - im.shape[1] + 1, 0)

    y1_pad = max(0, -y1)
    y2_pad = max(y2 - im.shape[0] + 1, 0)

    # Crop target
    im_crop = im[y1 + y1_pad:y2 - y2_pad, x1 + x1_pad:x2 - x2_pad, :]

    # Pad
    im_crop_padded = cv2.copyMakeBorder(im_crop, y1_pad, y2_pad, x1_pad, x2_pad, cv2.BORDER_CONSTANT)
    # deal with attention mask
    H, W, _ = im_crop_padded.shape
    att_mask = np.ones((H, W))
    end_x, end_y = -x2_pad, -y2_pad
    if y2_pad == 0:
        end_y = None
    if x2_pad == 0:
        end_x = None
    att_mask[y1_pad:end_y, x1_pad:end_x] = 0

    if output_sz is not None:
        resize_factor = output_sz / crop_sz
        im_crop_padded = cv2.resize(im_crop_padded, (output_sz, output_sz))
        att_mask = cv2.resize(att_mask, (output_sz, output_sz)).astype(np.bool_)
        return im_crop_padded, resize_factor, att_mask

    else:
        return im_crop_padded, att_mask.astype(np.bool_), 1.0


def clip_box(box: list, H, W, margin=0):
    x1, y1, w, h = box
    x2, y2 = x1 + w, y1 + h
    x1 = min(max(0, x1), W - margin)
    x2 = min(max(margin, x2), W)
    y1 = min(max(0, y1), H - margin)
    y2 = min(max(margin, y2), H)
    w = max(margin, x2 - x1)
    h = max(margin, y2 - y1)
    return [x1, y1, w, h]


def map_box_back(state, pred_box: list, resize_factor: float):
    cx_prev, cy_prev = state[0] + 0.5 * state[2], state[1] + 0.5 * state[3]
    cx, cy, w, h = pred_box
    search_size = 320
    half_side = 0.5 * search_size / resize_factor
    cx_real = cx + (cx_prev - half_side)
    cy_real = cy + (cy_prev - half_side)
    return [cx_real - 0.5 * w, cy_real - 0.5 * h, w, h]


def save_res(im_dir, data):
    file = os.path.join(prj_dir, "test_videos\\pred_boxes", os.path.split(im_dir)[1] + ".txt")
    tracked_bb = np.array(data).astype(int)
    np.savetxt(file, tracked_bb, delimiter='\t', fmt='%d')


def get_new_frame(frame_id, im_dir):
    imgs = [img for img in os.listdir(im_dir) if img.endswith(".jpg")]
    if len(imgs) <= frame_id:
        return None
    im = cv2.imread(os.path.join(im_dir, imgs[frame_id]))
    return cv2.cvtColor(im, cv2.COLOR_BGR2RGB)


def get_init_box(im_dir):
    path = os.path.join(im_dir, "groundtruth.txt")
    ground_truth_rect = np.loadtxt(path, delimiter=',', dtype=np.int)
    if len(ground_truth_rect.shape) == 2:
        return ground_truth_rect[0]
    return ground_truth_rect

def get_gt_box(im_dir):
    path = os.path.join(im_dir, "groundtruth.txt")
    ground_truth_rect = np.loadtxt(path, delimiter=',', dtype=np.float64)
    return ground_truth_rect

def get_abs_box(im_dir):
    path = os.path.join(im_dir, "absence.label")
    abs = np.loadtxt(path)
    return abs

def get_iou(gt, output, abs=None):
    n = min(gt.shape[0], output.shape[0])
    if abs is None:
        abs = np.zeros(n)
    iou = []
    for i in range(1, n):
        if int(abs[i]) == 0:
            x_l = max(gt[i][0], output[i][0])
            y_top = max(gt[i][1], output[i][1])
            x_r = min(gt[i][0] + gt[i][2], output[i][0] + output[i][2])
            y_bot = min(gt[i][1] + gt[i][3], output[i][1] + output[i][3])
            if x_r < x_l or y_bot < y_top:
                iou.append(0.0)
            else:
                inter = (x_r - x_l) * (y_bot - y_top)
                iou.append(inter / (gt[i][2] * gt[i][3] + output[i][2] * output[i][3] - inter))
    # print(iou)
    return np.mean(iou)


def my_tracker(get_new_frame, get_init_box, im_dir):
    params = edict()
    # template and search region
    params.template_factor = 2.0
    params.template_size = 128
    params.search_factor = 5.0
    params.search_size = 320
    update_intervals = [1, 15]
    num_extra_template = len(update_intervals)
    frame_id = 0
    z_dict_list = []
    state = get_init_box(im_dir)
    image = get_new_frame(frame_id, im_dir)
    z_patch_arr, _, z_amask_arr = sample_target(image, state, params.template_factor, output_sz=params.template_size)
    # print(z_patch_arr)
    #template = process(z_patch_arr, z_amask_arr)
    # forward the template once
    backbone = torch.jit.load('stark_st_backbone.pt')
    backbone.eval()
    transformer = torch.jit.load('stark_st_transformer.pt')
    transformer.eval()
    z_dict1 = backbone(torch.tensor(z_patch_arr), torch.tensor(z_amask_arr, dtype=torch.bool))
    z_dict_list.append(z_dict1)
    for i in range(num_extra_template):
        z_dict_list.append(z_dict1)
    outputs = []
    outputs.append(state)
    frame_id += 1
    image = get_new_frame(frame_id, im_dir)
    while image is not None:
        H, W, _ = image.shape
        # get the t-th search region
        x_patch_arr, resize_factor, x_amask_arr = sample_target(image, state, params.search_factor,
                                                                output_sz=params.search_size)  # (x1, y1, w, h)
        '''
        print(x_patch_arr.shape)
        print(x_amask_arr.shape)
        search = process(x_patch_arr, x_amask_arr)
        '''
        x_dict = backbone(torch.tensor(x_patch_arr), torch.tensor(x_amask_arr, dtype=torch.bool))
        # merge the template and the search
        feat_dict_list = z_dict_list + [x_dict]
        seq_dict = merge_template_search(feat_dict_list)
        # run the transformer
        out_dict, _, _ = transformer(seq_dict["feat"], seq_dict["mask"], seq_dict["pos"], run_box_head=True,
                                     run_cls_head=True)
        # get the final result
        pred_boxes = out_dict['pred_boxes'].view(-1, 4)
        # Baseline: Take the mean of all pred boxes as the final result
        pred_box = (pred_boxes.mean(dim=0) * params.search_size / resize_factor).tolist()  # (cx, cy, w, h) [0,1]
        # get the final box result
        state = clip_box(map_box_back(state=state, pred_box=pred_box, resize_factor=resize_factor), H, W, margin=10)
        # get confidence score (whether the search region is reliable)
        conf_score = out_dict["pred_logits"].view(-1).sigmoid().item()
        # update template
        for idx, update_i in enumerate(update_intervals):
            if frame_id % update_i == 0 and conf_score > 0.5:
                z_patch_arr, _, z_amask_arr = sample_target(image, state, params.template_factor,
                                                            output_sz=params.template_size)  # (x1, y1, w, h)
                #template_t = process(z_patch_arr, z_amask_arr)
                with torch.no_grad():
                    z_dict_t = backbone(torch.tensor(z_patch_arr), torch.tensor(z_amask_arr, dtype=torch.bool))
                z_dict_list[idx + 1] = z_dict_t  # the 1st element of z_dict_list is template from the 1st frame

        outputs.append(state)
        frame_id += 1
        image = get_new_frame(frame_id, im_dir)
    return outputs


'''
Script arguments: 
1) Path to the folder - full path to the directory with images with .jpg extension; image names are numbers in increasing order.
Directory should also contain the file groundtruth.txt with the position of the object at the initial 1st frame.
2) backend: onnx or tensorflow
'''


def main():
    parser = argparse.ArgumentParser(description='Run tracker on sequence or dataset.')
    parser.add_argument('folder_path', type=str, default=None, help='Path to the folder')
    args = parser.parse_args()
    has_folders = 0
    for f in os.listdir(args.folder_path):
        if os.path.isdir(os.path.join(args.folder_path, f)):
            has_folders = 1
            break
    if has_folders == 1:
        iou = []
        for f in os.listdir(args.folder_path):
            print(f)
            path = os.path.join(args.folder_path, f)
            outputs = my_tracker(get_new_frame, get_init_box, path)
            save_res(path, outputs)
            a = get_iou(get_gt_box(path), np.array(outputs), get_abs_box(path))
            iou.append(a)
            print(a)
        print(np.mean(iou))
    else:
        outputs = my_tracker(get_new_frame, get_init_box, args.folder_path)
        save_res(args.folder_path, outputs)
        print(get_iou(get_gt_box(args.folder_path), np.array(outputs), get_abs_box(args.folder_path)))


if __name__ == '__main__':
    main()