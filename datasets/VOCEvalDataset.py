# ----------------------------------------
# The dataset for Eval that is used for label correction
# ----------------------------------------

from __future__ import print_function, division

import multiprocessing
import os

import pandas as pd
import torch.nn.functional as F

from datasets.BaseMultiwGTauginfoDataset import BaseMultiwGTauginfoDataset
from datasets.transformmultiGT import *
from utils.imutils import *
from utils.iou_computation import update_iou_stat, compute_iou
from utils.registry import DATASETS


@DATASETS.register_module
class VOCEvalDataset(BaseMultiwGTauginfoDataset):
    def __init__(self, cfg, period, transform='none'):
        super(VOCEvalDataset, self).__init__(cfg, period, transform)
        self.dataset_name = 'VOC%d' % cfg.DATA_YEAR
        self.root_dir = os.path.join(cfg.ROOT_DIR, 'VOCdevkit')
        self.dataset_dir = os.path.join(self.root_dir, self.dataset_name)
        self.rst_dir = os.path.join(self.root_dir, 'results', self.dataset_name, 'Segmentation')
        self.eval_dir = os.path.join(self.root_dir, 'eval_result', self.dataset_name, 'Segmentation')
        self.img_dir = os.path.join(self.dataset_dir, 'JPEGImages')
        # print(self.img_dir)
        self.ann_dir = os.path.join(self.dataset_dir, 'Annotations')
        self.seg_dir = os.path.join(self.dataset_dir, 'SegmentationClass')
        self.seg_dir_gt = os.path.join(self.dataset_dir, 'SegmentationClassAug')
        self.set_dir = os.path.join(self.dataset_dir, 'ImageSets', 'Segmentation')
        if cfg.DATA_PSEUDO_GT:
            self.pseudo_gt_dir = cfg.DATA_PSEUDO_GT
        # self.pseudo_gt_dir_2 = cfg.DATA_PSEUDO_GT_2
        # self.pseudo_gt_dir_3 = cfg.DATA_PSEUDO_GT_3
        else:
            self.pseudo_gt_dir = os.path.join(self.root_dir, 'pseudo_gt', self.dataset_name, 'Segmentation')

        file_name = None
        if cfg.DATA_AUG and 'train' in self.period:
            file_name = self.set_dir + '\\' + period + 'aug.txt'
        else:
            file_name = self.set_dir + '\\' + period + '.txt'
        df = pd.read_csv(file_name, names=['filename'])
        self.name_list = df['filename'].values
        # print(self.name_list[1])
        if self.dataset_name == 'VOC2012':
            self.categories = ['aeroplane', 'bicycle', 'bird', 'boat', 'bottle', 'bus', 'car', 'cat', 'chair', 'cow',
                               'diningtable', 'dog', 'horse', 'motorbike', 'person', 'pottedplant', 'sheep', 'sofa',
                               'train', 'tvmonitor']
            self.coco2voc = [[0], [5], [2], [16], [9], [44], [6], [3], [17], [62],
                             [21], [67], [18], [19], [4], [1], [64], [20], [63], [7], [72]]

            self.num_categories = len(self.categories) + 1
            self.cmap = self.__colormap(len(self.categories) + 1)

        # to record the previous prediction
        self.prev_pred_dict = {}

        self.ori_indx_list = []

    def __len__(self):
        return len(self.name_list)

    def __getitem__(self, idx):
        sample = self.__sample_generate__(idx)
        if 'segmentation' in sample.keys():
            sample['mask'] = sample['segmentation'] < self.num_categories
            t = sample['segmentation'].copy()
            t[t >= self.num_categories] = 0
            sample['segmentation_onehot'] = onehot(t, self.num_categories)
        return self.totensor(sample)

    def __sample_generate__(self, idx, split_idx=0):
        name = self.load_name(idx)
        image = self.load_image(idx)
        r, c, _ = image.shape
        sample = {'image': image, 'name': name, 'row': r, 'col': c, 'batch_idx': idx}

        if 'test' in self.period:
            return self.__transform__(sample)
        elif self.cfg.DATA_PSEUDO_GT and idx >= split_idx and 'train' in self.period:
            segmentation, seg_gt = self.load_pseudo_segmentation(idx)
        else:
            segmentation = self.load_segmentation(idx)

        sample['segmentation'] = segmentation
        t = sample['segmentation'].copy()
        t[t >= self.num_categories] = 0
        sample['category'] = seg2cls(t, self.num_categories)
        sample['category_copypaste'] = np.zeros(sample['category'].shape)

        # if there is previous prediction for this video
        if idx in self.prev_pred_dict.keys():
            # interpolate to the image spatial resolution self.prev_pred_dict[idx] size 1,c,h,w
            if torch.is_tensor(self.prev_pred_dict[idx]):
                # prev_pred = F.interpolate(self.prev_pred_dict[idx], size=(r, c), mode='nearest')
                prev_pred = F.interpolate(self.prev_pred_dict[idx], size=(r, c), mode='bilinear', align_corners=True,
                                          recompute_scale_factor=False)
            else:
                # prev_pred = F.interpolate(torch.tensor(self.prev_pred_dict[idx]), size=(r, c), mode='nearest')
                prev_pred = F.interpolate(torch.tensor(self.prev_pred_dict[idx]), size=(r, c), mode='bilinear',
                                          align_corners=True,
                                          recompute_scale_factor=False)
            sample['prev_prediction'] = prev_pred  # 1,c,h,w

        # the small scale case
        # sample['segmentation2'] = segmentation2
        # sample['segmentation3'] = segmentation3

        sample['segmentationgt'] = seg_gt

        if self.transform == 'none' and self.cfg.DATA_FEATURE_DIR:
            feature = self.load_feature(idx)
            sample['feature'] = feature
        return self.__transform__(sample)

    def load_name(self, idx):
        name = self.name_list[idx]
        return name

    def load_image(self, idx):
        name = self.name_list[idx]
        img_file = self.img_dir + '/' + name + '.jpg'
        image = cv2.imread(img_file)
        image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        return image_rgb

    def load_segmentation(self, idx):
        name = self.name_list[idx]
        seg_file = self.seg_dir + '/' + name + '.png'
        segmentation = np.array(Image.open(seg_file))
        return segmentation

    def load_pseudo_segmentation(self, idx):
        name = self.name_list[idx]
        seg_file = self.pseudo_gt_dir + '/' + name + '.png'

        segmentation1 = Image.open(seg_file)
        width, height = segmentation1.size

        segmentation1 = np.array(segmentation1)

        seg_gt_file = self.seg_dir_gt + '/' + name + '.png'
        seg_gt = np.array(Image.open(seg_gt_file).resize((width, height)))

        return segmentation1, seg_gt

    def __colormap(self, N):
        """Get the map from label index to color

        Args:
            N: number of class

            return: a Nx3 matrix

        """
        cmap = np.zeros((N, 3), dtype=np.uint8)

        def uint82bin(n, count=8):
            """returns the binary of integer n, count refers to amount of bits"""
            return ''.join([str((n >> y) & 1) for y in range(count - 1, -1, -1)])

        for i in range(N):
            r = 0
            g = 0
            b = 0
            idx = i
            for j in range(7):
                str_id = uint82bin(idx)
                r = r ^ (np.uint8(str_id[-1]) << (7 - j))
                g = g ^ (np.uint8(str_id[-2]) << (7 - j))
                b = b ^ (np.uint8(str_id[-3]) << (7 - j))
                idx = idx >> 3
            cmap[i, 0] = r
            cmap[i, 1] = g
            cmap[i, 2] = b
        return cmap

    def load_ranked_namelist(self):
        df = self.read_rank_result()
        self.name_list = df['filename'].values

    def label2colormap(self, label):
        m = label.astype(np.uint8)
        r, c = m.shape
        cmap = np.zeros((r, c, 3), dtype=np.uint8)
        cmap[:, :, 0] = (m & 1) << 7 | (m & 8) << 3
        cmap[:, :, 1] = (m & 2) << 6 | (m & 16) << 2
        cmap[:, :, 2] = (m & 4) << 5
        cmap[m == 255] = [255, 255, 255]
        return cmap

    def save_result(self, result_list, model_id):
        """Save test results

        Args:
            result_list(list of dict): [{'name':name1, 'predict':predict_seg1},{...},...]

        """
        folder_path = os.path.join(self.rst_dir, '%s_%s' % (model_id, self.period))
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)

        for sample in result_list:
            file_path = os.path.join(folder_path, '%s.png' % sample['name'])
            cv2.imwrite(file_path, sample['predict'])

    def save_pseudo_gt(self, result_list, folder_path=None):
        """Save pseudo gt

        Args:
            result_list(list of dict): [{'name':name1, 'predict':predict_seg1},{...},...]

        """
        i = 1
        folder_path = self.pseudo_gt_dir if folder_path is None else folder_path
        if not os.path.exists(folder_path):
            os.makedirs(folder_path)
        for sample in result_list:
            file_path = os.path.join(folder_path, '%s.png' % (sample['name']))
            cv2.imwrite(file_path, sample['predict'])
            i += 1

    def do_matlab_eval(self, model_id):
        import subprocess
        path = os.path.join(self.root_dir, 'VOCcode')
        eval_filename = os.path.join(self.eval_dir, '%s_result.mat' % model_id)
        cmd = 'cd {} && '.format(path)
        cmd += 'matlab -nodisplay -nodesktop '
        cmd += '-r "dbstop if error; VOCinit; '
        cmd += 'VOCevalseg(VOCopts,\'{:s}\');'.format(model_id)
        cmd += 'accuracies,avacc,conf,rawcounts = VOCevalseg(VOCopts,\'{:s}\'); '.format(model_id)
        cmd += 'save(\'{:s}\',\'accuracies\',\'avacc\',\'conf\',\'rawcounts\'); '.format(eval_filename)
        cmd += 'quit;"'

        print('start subprocess for matlab evaluation...')
        print(cmd)
        subprocess.call(cmd, shell=True)

    def do_python_eval(self, model_id):
        predict_folder = os.path.join(self.rst_dir, '%s_%s' % (model_id, self.period))
        gt_folder = self.seg_dir
        TP = []
        P = []
        T = []
        for i in range(self.num_categories):
            TP.append(multiprocessing.Value('i', 0, lock=True))
            P.append(multiprocessing.Value('i', 0, lock=True))
            T.append(multiprocessing.Value('i', 0, lock=True))

        def compare(start, step, TP, P, T):
            for idx in range(start, len(self.name_list), step):
                # print('%d/%d'%(idx,len(self.name_list)))
                name = self.name_list[idx]
                predict_file = os.path.join(predict_folder, '%s.png' % name)
                gt_file = os.path.join(gt_folder, '%s.png' % name)
                predict = np.array(Image.open(predict_file))  # cv2.imread(predict_file)
                gt = np.array(Image.open(gt_file))
                cal = gt < 255
                mask = (predict == gt) * cal

                for i in range(self.num_categories):
                    P[i].acquire()
                    P[i].value += np.sum((predict == i) * cal)
                    P[i].release()
                    T[i].acquire()
                    T[i].value += np.sum((gt == i) * cal)
                    T[i].release()
                    TP[i].acquire()
                    TP[i].value += np.sum((gt == i) * mask)
                    TP[i].release()

        p_list = []
        for i in range(8):
            p = multiprocessing.Process(target=compare, args=(i, 8, TP, P, T))
            p.start()
            p_list.append(p)
        for p in p_list:
            p.join()
        IoU = []
        for i in range(self.num_categories):
            IoU.append(TP[i].value / (T[i].value + P[i].value - TP[i].value + 1e-10))
        loglist = {}
        for i in range(self.num_categories):
            if i == 0:
                print('%11s:%7.3f%%' % ('background', IoU[i] * 100), end='\t')
                loglist['background'] = IoU[i] * 100
            else:
                if i % 2 != 1:
                    print('%11s:%7.3f%%' % (self.categories[i - 1], IoU[i] * 100), end='\t')
                else:
                    print('%11s:%7.3f%%' % (self.categories[i - 1], IoU[i] * 100))
                loglist[self.categories[i - 1]] = IoU[i] * 100

        miou = np.mean(np.array(IoU))
        print('\n======================================================')
        print('%11s:%7.3f%%' % ('mIoU', miou * 100))
        loglist['mIoU'] = miou * 100
        return loglist

    def do_python_eval_batch_pseudo_one_process(self):
        self.seg_dir_gt = os.path.join(self.dataset_dir, 'SegmentationClassAug')
        gt_folder = self.seg_dir_gt
        TP_gt_epoch = [0] * 21
        P_gt_epoch = [0] * 21
        T_gt_epoch = [0] * 21
        loglist = {}
        for idx in range(len(self.name_list)):
            # print(idx)
            name = self.name_list[idx]
            gt_file = os.path.join(gt_folder, '%s.png' % name)
            gt = np.array(Image.open(gt_file))
            r, c = gt.shape
            # print(r)
            predict_tensor = F.interpolate(self.prev_pred_dict[idx], size=(r, c), mode='bilinear', align_corners=True,
                                           recompute_scale_factor=False)  # 1,c,h,w
            predict = predict_tensor[0].cpu().numpy()  # c,h,w
            predict = np.argmax(predict, axis=0)  # h,w

            TP_gt_epoch, P_gt_epoch, T_gt_epoch = update_iou_stat(predict, gt, TP_gt_epoch,
                                                                  P_gt_epoch, T_gt_epoch)
        IoU_gt_epoch = compute_iou(TP_gt_epoch, P_gt_epoch, T_gt_epoch)
        for indx, class_name in enumerate(
                ['background', 'aeroplane', 'bicycle', 'bird', 'boat', 'bottle', 'bus', 'car', 'cat', 'chair',
                 'cow',
                 'diningtable', 'dog', 'horse', 'motorbike', 'person', 'pottedplant', 'sheep', 'sofa', 'train',
                 'tvmonitor']):
            loglist[class_name] = IoU_gt_epoch[indx]
        mIoU_clean_epoch = np.mean(np.array(IoU_gt_epoch))
        loglist['mIoU'] = mIoU_clean_epoch
        return loglist

    def __coco2voc(self, m):
        r, c = m.shape
        result = np.zeros((r, c), dtype=np.uint8)
        for i in range(0, 21):
            for j in self.coco2voc[i]:
                result[m == j] = i
        return result
