import os
import cv2
import math
import time
import torch
import torch.distributed as dist
import numpy as np
import random
import argparse

from model.model import Model
from dataset import *
from torch.utils.data import DataLoader, Dataset
from torch.utils.tensorboard import SummaryWriter
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm
import sys

device = torch.device("cuda")

log_path = 'train_log_240425'

def get_learning_rate(step):
    if step < 2000:
        mul = step / 2000.
        return 2e-4 * mul
    else:
        mul = np.cos((step - 2000) / (args.epoch * args.step_per_epoch - 2000.) * math.pi) * 0.5 + 0.5
        return (2e-4 - 2e-6) * mul + 2e-6

def flow2rgb(flow_map_np):
    h, w, _ = flow_map_np.shape
    rgb_map = np.ones((h, w, 3)).astype(np.float32)
    normalized_flow_map = flow_map_np / (np.abs(flow_map_np).max())
    
    rgb_map[:, :, 0] += normalized_flow_map[:, :, 0]
    rgb_map[:, :, 1] -= 0.5 * (normalized_flow_map[:, :, 0] + normalized_flow_map[:, :, 1])
    rgb_map[:, :, 2] += normalized_flow_map[:, :, 1]
    return rgb_map.clip(0, 1)

def tensor2image(img):
    img = img.permute(1, 2, 0).cpu().numpy()
    return (img * 255).astype('uint8')

def rgb2y(img):
    return 0.299 * img[:, :, 0] + 0.587 * img[:, :, 1] + 0.114 * img[:, :, 2]

def train(model, local_rank):
    if local_rank == 0:
        writer = SummaryWriter(f'exp/{args.exp_name}/train')
        writer_val = SummaryWriter(f'exp/{args.exp_name}/validate')
    else:
        writer = None
        writer_val = None
    step = 0
    nr_eval = 0
    dataset = AdobeDataset('train', args.root_dir)
    sampler = DistributedSampler(dataset)
    train_data = DataLoader(dataset, batch_size=args.batch_size, num_workers=2, pin_memory=True, drop_last=True, sampler=sampler)
    args.step_per_epoch = train_data.__len__()
    dataset_val = AdobeDataset('test', args.root_dir)
    val_data = DataLoader(dataset_val, batch_size=args.batch_size, pin_memory=True, num_workers=2)
    print('training...')
    time_stamp = time.time()
    for epoch in range(args.epoch):
        sampler.set_epoch(epoch)

        if nr_eval % 1 == 0:
            evaluate(model, val_data, step, local_rank, writer_val, current_epoch=epoch)
        dist.barrier()

        # train with mixed precision

        psnr_list = []
        for i, data in enumerate(train_data):
            data_time_interval = time.time() - time_stamp
            time_stamp = time.time()
            data_gpu, lowres, timestep = data
            data_gpu = data_gpu.to(device, non_blocking=True) / 255.
            lowres = lowres.to(device, non_blocking=True) / 255.
            timestep = timestep.to(device, non_blocking=True)
            imgs = data_gpu
            lowres = torch.cat((lowres[:, :3], lowres[:, 6:9]), 1)
            gt = data_gpu[:, 3:6]
            learning_rate = get_learning_rate(step) * args.world_size / 4
            pred, info = model.update(imgs, lowres, learning_rate, timestep, training=True) # pass timestep if you are training RIFEm
            pred = pred[1]
            train_time_interval = time.time() - time_stamp
            time_stamp = time.time()
            if step % 200 == 1 and local_rank == 0:
                writer.add_scalar('learning_rate', learning_rate, step)
                writer.add_scalar('loss/l1', info['loss_l1'], step)
                # for j in range(8):
                #     writer.add_scalar('scale/{}'.format(j), np.mean(scale[:, j]), step)
                for j in range(gt.shape[0]):
                    psnr = -10 * math.log10(1e-6 + torch.mean((gt[j] - pred[j]) * (gt[j] - pred[j])).cpu().data)
                    psnr_list.append(psnr)
                if len(psnr_list) > 200:
                    psnr_list = psnr_list[-200:]
                writer.add_scalar('psnr', np.array(psnr_list).mean(), step)
            if step % 1000 == 1 and local_rank == 0:
                gt = (gt.permute(0, 2, 3, 1).detach().cpu().numpy() * 255).astype('uint8')
                pred = (pred.permute(0, 2, 3, 1).detach().cpu().numpy() * 255).astype('uint8')
                flow = info['flow'].permute(0, 2, 3, 1).detach().cpu().numpy()
                for i in range(5):
                    imgs = np.concatenate((pred[i], gt[i]), 1)[:, :, ::-1]
                    writer.add_image(str(i) + '/img', imgs, step, dataformats='HWC')
                    writer.add_image(str(i) + '/flow', flow2rgb(flow[i]), step, dataformats='HWC')
                writer.flush()
            if local_rank == 0:
                # print('epoch:{} {}/{} time:{:.2f}+{:.2f} loss_l1:{:.4e}'.format(epoch, i, args.step_per_epoch, data_time_interval, train_time_interval, info['loss_l1']))
                # update using tqdm
                sys.stdout.write('\repoch:{} {}/{} time:{:.2f}+{:.2f} loss_l1:{:.4e}'.format(epoch, i, args.step_per_epoch, data_time_interval, train_time_interval, info['loss_l1']))
                sys.stdout.flush()

            step += 1
        model.save_model(log_path, local_rank)
        dist.barrier()
        nr_eval += 1

# def evaluate(model, val_data, nr_eval, local_rank, writer_val):
#     loss_l1_list = []
#     psnr_list = []
#     time_stamp = time.time()
#     for i, data in tqdm(enumerate(val_data), total=len(val_data)):
#         if i > 10:
#             break
#         imgs, lowres, timestep = data
#         imgs = imgs.to(device, non_blocking=True) / 255.
#         lowres = lowres.to(device, non_blocking=True) / 255.
#         i1, i2, i3 = imgs.chunk(3, dim=1)
#         l1, l2, l3 = lowres.chunk(3, dim=1)
#         imgs = imgs.chunk(3, dim=1)
#         with torch.no_grad():
#             res, info = model.update(torch.cat((i1, i2, i3), 1), torch.cat((l1, l3), 1), training=False)
#         loss_l1_list.append(info['loss_l1'].cpu().numpy())
#         for j in range(res[0].shape[0]):
#             psnr_all = []
#             for k in range(3):
#                 # pred_y = rgb2y(res[k][j].permute(1, 2, 0))
#                 # gt_y = rgb2y(imgs[k][j].permute(1, 2, 0))
#                 pred_y = res[k][j].permute(1, 2, 0)
#                 gt_y = imgs[k][j].permute(1, 2, 0)
#                 psnr_all.append(-10 * math.log10(torch.mean((gt_y - pred_y) * (gt_y - pred_y)).cpu().data))
#             psnr_list.append(np.array(psnr_all).mean())
#         flow0 = info['flow'].permute(0, 2, 3, 1).cpu().numpy()
#         if i == 0 and local_rank == 0:
#             k = 1
#             gt = (imgs[k].permute(0, 2, 3, 1).cpu().numpy() * 255).astype('uint8')
#             pred = (res[k].permute(0, 2, 3, 1).cpu().numpy() * 255).astype('uint8')
#             for j in range(pred.shape[0]):
#                 # imgs = np.concatenate((pred[j], gt[j]), 3)[:, :, ::-1]
#                 # writer_val.add_image(str(j) + '/img{}'.format(k), imgs.copy(), nr_eval, dataformats='HWC')
#                 writer_val.add_image(str(j) + '/flow', flow2rgb(flow0[j][:, :, ::-1]), nr_eval, dataformats='HWC')
#
#     eval_time_interval = time.time() - time_stamp
#
#     if local_rank == 0:
#         print(args.exp_name, np.array(psnr_list).mean())
#         writer_val.add_scalar('{}_psnr'.format(args.exp_name), np.array(psnr_list).mean(), nr_eval)


def evaluate(model, val_data, nr_eval, local_rank, writer_val, current_epoch):
    loss_l1_list = []
    psnr_list = []
    time_stamp = time.time()
    for i, data in enumerate(tqdm(val_data)):
        if i > 10:
            break
        imgs, lowres, timestep = data
        imgs = imgs.to(device, non_blocking=True) / 255.
        lowres = lowres.to(device, non_blocking=True) / 255.
        high_res_list = imgs.chunk(9, dim=1)
        low_res_list = lowres.chunk(9, dim=1)
        imgs = imgs.chunk(3, dim=1)
        with torch.no_grad():
            # results = model.inference(low_res_list[0], low_res_list[-1], timestep=[0, 1/8, 2/8, 3/8, 4/8, 5/8, 6/8, 7/8, 1])
            res, info = model.update(torch.cat(high_res_list, 1), torch.cat((low_res_list[0], low_res_list[-1]), 1), training=False)

        for pred_img, high_res_img in zip(res, high_res_list):
            # calculate psnr between pred_img and high_res_list. pred_img size is [batch, 3, H, W]
            psnr_all = []
            for k in range(pred_img.shape[0]):
                pred_y = pred_img[k].permute(1, 2, 0) * 255
                gt_y = high_res_img[k].permute(1, 2, 0) * 255
                # convert to uint8, not convert to numpy
                pred_y, gt_y = pred_y.type(torch.uint8).type(torch.float32), gt_y.type(torch.uint8).type(torch.float32)
                psnr_all.append(-10 * math.log10(torch.mean((gt_y - pred_y) * (gt_y - pred_y)).cpu().data))

                # concat visualization (concat)
                combined_image = np.concatenate((pred_y.cpu().numpy(), gt_y.cpu().numpy()), axis=1)
                os.makedirs('./result', exist_ok=True)
                cv2.imwrite(f'./result/epoch{current_epoch:03d}_{nr_eval}_{i}_{k}.png', combined_image)

            psnr_list.append(np.array(psnr_all).mean())

        # loss_l1_list.append(info['loss_l1'].cpu().numpy())
    # print('eval time:', time.time() - time_stamp)
    if local_rank == 0:
        print("PSNR : ", args.exp_name, np.array(psnr_list).mean())

def get_current_time():
    return time.strftime('%Y%m%d_%H%M%S', time.localtime(time.time()))
        
if __name__ == "__main__":    
    parser = argparse.ArgumentParser()
    parser.add_argument('--epoch', default=220, type=int)
    parser.add_argument('--batch_size', default=6, type=int, help='minibatch size')
    parser.add_argument('--local_rank', default=0, type=int, help='local rank')
    parser.add_argument('--world_size', default=1, type=int, help='world size')
    parser.add_argument('--exp_name', default=get_current_time(), type=str, help='experiment name')
    parser.add_argument('--root_dir', default="/media/ms-neo2/ms-ssd11/1.dataset/VFI/adobe240fps", type=str, help='root directory')
    args = parser.parse_args()
    torch.distributed.init_process_group(backend="nccl", world_size=args.world_size)
    torch.cuda.set_device(args.local_rank)
    seed = 1234
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.benchmark = True
    model = Model(args.local_rank)
    model.load_model('train_log_240425', rank=-1) # module.WEIGHT
    train(model, args.local_rank)
        
