import argparse
import logging
import sys
from pathlib import Path

import torch
from torch.functional import Tensor
import torch.nn as nn
import torch.nn.functional as F
import wandb
from torch import optim
from torch.utils.data import DataLoader, dataset, random_split
from tqdm import tqdm

from utils.data_loading import BasicDataset, CarvanaDataset, PimgDataset
from utils.dice_score import dice_loss
from evaluate import evaluate
from unet import UNet

# dir_img = Path('./data/imgs/')
# dir_mask = Path('./data/masks/')
# dir_img = Path('../../../../data/floorplan/selflabel/imgs/')
# dir_mask = Path('../../../../data/floorplan/selflabel/masks/')

dir_img = Path('../../../../data/floorplan/pimg/imgs/')
dir_pimg = Path('../../../../data/floorplan/pimg/JPEG-DOP1/')
dir_mask = Path('../../../../data/floorplan/pimg/masks/')

dir_checkpoint = Path('./checkpoints/')


def train_net(net,
              device,
              epochs: int = 5,
              batch_size: int = 1,
              learning_rate: float = 0.001,
              val_percent: float = 0.1,
              save_checkpoint: bool = True,
              img_scale: float = 0.5,
              amp: bool = False):
    # 1. Create dataset
    # try:
    #     dataset = CarvanaDataset(dir_img, dir_mask, img_scale)
    # except (AssertionError, RuntimeError):
    #     dataset = BasicDataset(dir_img, dir_mask, img_scale)
    dataset=PimgDataset(dir_img,dir_pimg,dir_mask,img_scale)

    # 2. Split into train / validation partitions
    n_val = int(len(dataset) * val_percent)
    n_train = len(dataset) - n_val
    train_set, val_set = random_split(dataset, [n_train, n_val], generator=torch.Generator().manual_seed(0))

    # 3. Create data loaders
    loader_args = dict(batch_size=batch_size, num_workers=4, pin_memory=True)
    train_loader = DataLoader(train_set, shuffle=True, **loader_args)
    val_loader = DataLoader(val_set, shuffle=False, drop_last=True, **loader_args)

    # (Initialize logging)
    experiment = wandb.init(project='U-Net', resume='allow', anonymous='must')
    experiment.config.update(dict(epochs=epochs, batch_size=batch_size, learning_rate=learning_rate,
                                  val_percent=val_percent, save_checkpoint=save_checkpoint, img_scale=img_scale,
                                  amp=amp))

    logging.info(f'''Starting training:
        Epochs:          {epochs}
        Batch size:      {batch_size}
        Learning rate:   {learning_rate}
        Training size:   {n_train}
        Validation size: {n_val}
        Checkpoints:     {save_checkpoint}
        Device:          {device.type}
        Images scaling:  {img_scale}
        Mixed Precision: {amp}
    ''')

    # 4. Set up the optimizer, the loss, the learning rate scheduler and the loss scaling for AMP
    optimizer = optim.RMSprop(net.parameters(), lr=learning_rate, weight_decay=1e-8, momentum=0.9) # momentum=0.99
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'max', patience=100)  # goal: maximize Dice score
    grad_scaler = torch.cuda.amp.GradScaler(enabled=amp)
    # criterion = nn.CrossEntropyLoss()
    criterion = nn.BCEWithLogitsLoss()
    global_step = 0

    # 5. Begin training
    for epoch in range(epochs):
        net.train()
        epoch_loss = 0
        with tqdm(total=n_train, desc=f'Epoch {epoch + 1}/{epochs}', unit='img') as pbar:
            for batch in train_loader:
                images = batch['image']
                true_masks = batch['mask']

                # warm up LR
                if global_step>5000:
                    optimizer = optim.RMSprop(net.parameters(), lr=0.000001, weight_decay=1e-8, momentum=0.9)
                    # if global_step>2000:
                    #     optimizer = optim.RMSprop(net.parameters(), lr=0.0000001, weight_decay=1e-8, momentum=0.9)

                assert images.shape[1] == net.n_channels, \
                    f'Network has been defined with {net.n_channels} input channels, ' \
                    f'but loaded images have {images.shape[1]} channels. Please check that ' \
                    'the images are loaded correctly.'

                images = images.to(device=device, dtype=torch.float32)
                # true_masks = true_masks.to(device=device, dtype=torch.long)
                true_masks = true_masks.to(device=device, dtype=torch.float32)
                true_masks = F.one_hot(true_masks.argmax(dim=1), net.n_classes).permute(0, 3, 1, 2).float()

                with torch.cuda.amp.autocast(enabled=amp):
                    masks_pred = net(images)
                    # loss=crossEntropy+dice
                    # loss = criterion(masks_pred, true_masks) \
                    #        + dice_loss(F.softmax(masks_pred, dim=1).float(),
                    #                    F.one_hot(true_masks, net.n_classes).permute(0, 3, 1, 2).float(),
                    #                    multiclass=True)
                    BCE_loss=criterion(masks_pred, true_masks) 

                    # test dice
                    # print()
                    # print('true_masks.shape: ',true_masks.shape)
                    # print('true_masks.numpy(): ',Tensor.cpu(true_masks).numpy())

                    # print()
                    # print('masks_pred.shape: ',masks_pred.shape)
                    # print('masks_pred.numpy(): ',Tensor.cpu(masks_pred).detach().numpy())
                    masks_pred_softmax = F.softmax(masks_pred, dim=1).float()
                    # print()
                    # print('masks_pred_softmax.shape: ',masks_pred_softmax.shape)
                    # print('masks_pred_softmax.numpy(): ',Tensor.cpu(masks_pred_softmax).detach().numpy())
                    masks_pred_max=masks_pred_softmax.argmax(dim=1)
                    # print()
                    # print('masks_pred_max.shape: ',masks_pred_max.shape)
                    # print('masks_pred_max.numpy(): ',Tensor.cpu(masks_pred_max).detach().numpy())
                    mask_pred_onehot = F.one_hot(masks_pred_max,net.n_classes).permute(0, 3, 1, 2).float()
                    # print()
                    # print('mask_pred_onehot.shape: ',mask_pred_onehot.shape)
                    # print('mask_pred_onehot.numpy(): ',Tensor.cpu(mask_pred_onehot).detach().numpy())
                    diceloss=dice_loss(masks_pred_softmax,true_masks.float(),multiclass=True)
                    # print()
                    # print('diceloss.shape: ',diceloss.shape)
                    # print('diceloss.numpy(): ',Tensor.cpu(diceloss).detach().numpy())
                    # diceloss=diceloss.requires_grad_()
                    BCE_dice_loss = BCE_loss+ diceloss

                    # change loss func here
                    loss=BCE_dice_loss
                    # print(loss)

                    # loss=dice
                    # loss = dice_loss(F.softmax(masks_pred, dim=1).float(),
                    #                    F.one_hot(true_masks, net.n_classes).permute(0, 3, 1, 2).float(),
                    #                    multiclass=True)
                    # print('---------------------------------------------------',Tensor.cpu(diceloss).numpy())
                optimizer.zero_grad(set_to_none=True)
                grad_scaler.scale(loss).backward()# change loss
                grad_scaler.step(optimizer)
                grad_scaler.update()

                pbar.update(images.shape[0])
                global_step += 1
                epoch_loss += loss.item()# change loss
                experiment.log({
                    'BCE_loss': BCE_loss.item(),
                    'dice_loss': diceloss.item(),
                    'BCE_dice_loss': BCE_dice_loss.item(),
                    # 'step': global_step,
                    # 'epoch': epoch
                })
                pbar.set_postfix(**{'loss (batch)': loss.item()}) # change loss

                # Evaluation round
                super_para=2 # 10
                if global_step % (n_train // (super_para * batch_size)) == 0:
                    histograms = {}
                    for tag, value in net.named_parameters():
                        tag = tag.replace('/', '.')
                        histograms['Weights/' + tag] = wandb.Histogram(value.data.cpu())
                        histograms['Gradients/' + tag] = wandb.Histogram(value.grad.data.cpu())

                    # val_score,val_score_soft,acc = evaluate(net, val_loader, device)
                    dice_softmax_nobg ,dice_softmax_bg,dice_onehot_nobg,dice_onehot_bg,acc = evaluate(net, val_loader, device)
                    val_score=dice_onehot_bg
                    # scheduler.step(val_score)

                    logging.info('Validation Dice score: {}'.format(val_score))
                    experiment.log({
                        'learning rate': optimizer.param_groups[0]['lr'],
                        'Dice softmax nobg': dice_softmax_nobg,
                        'Dice softmax bg': dice_softmax_bg,
                        'Dice onehot nobg': dice_onehot_nobg,
                        'Dice onehot bg': dice_onehot_bg,
                        'PA': acc,
                        'images': wandb.Image(images[0].cpu()),
                        'masks': {
                            'true': wandb.Image(true_masks[0].float().cpu()),
                            'pred': wandb.Image(torch.softmax(masks_pred, dim=1)[0].float().cpu()),
                        },
                        # 'step': global_step,
                        # 'epoch': epoch,
                        **histograms
                    })
    # just save the last one
    if save_checkpoint:
        Path(dir_checkpoint).mkdir(parents=True, exist_ok=True)
        torch.save(net.state_dict(), 
        str(dir_checkpoint / 'checkpoint_epoch{}_pimg_BCEdice.pth'.format(epochs)))
        # logging.info(f'Checkpoint {epoch + 1} saved!')
        logging.info(f'Checkpoint {epochs} saved!')


def get_args():
    parser = argparse.ArgumentParser(description='Train the UNet on images and target masks')
    parser.add_argument('--epochs', '-e', metavar='E', type=int, default=100, help='Number of epochs')
    parser.add_argument('--batch-size', '-b', dest='batch_size', metavar='B', type=int, default=1, help='Batch size')
    parser.add_argument('--learning-rate', '-l', metavar='LR', type=float, default=0.00001,
                        help='Learning rate', dest='lr')
    parser.add_argument('--load', '-f', type=str, default=False, help='Load model from a .pth file')
    parser.add_argument('--scale', '-s', type=float, default=0.5, help='Downscaling factor of the images')
    parser.add_argument('--validation', '-v', dest='val', type=float, default=10.0,
                        help='Percent of the data that is used as validation (0-100)')
    parser.add_argument('--amp', action='store_true', default=False, help='Use mixed precision')

    return parser.parse_args()


if __name__ == '__main__':
    args = get_args()

    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    cuda_name='cuda:1' 
    device = torch.device(cuda_name if torch.cuda.is_available() else 'cpu')
    logging.info(f'Using device {device}')

    # Change here to adapt to your data
    # n_channels=3 for RGB images
    # n_classes is the number of probabilities you want to get per pixel
    net = UNet(n_channels=4, n_classes=3, bilinear=True)

    logging.info(f'Network:\n'
                 f'\t{net.n_channels} input channels\n'
                 f'\t{net.n_classes} output channels (classes)\n'
                 f'\t{"Bilinear" if net.bilinear else "Transposed conv"} upscaling')

    if args.load:
        net.load_state_dict(torch.load(args.load, map_location=device))
        logging.info(f'Model loaded from {args.load}')

    net.to(device=device)
    try:
        train_net(net=net,
                  epochs=args.epochs,
                  batch_size=args.batch_size,
                  learning_rate=args.lr,
                  device=device,
                  img_scale=args.scale,
                  val_percent=args.val / 100,
                  amp=args.amp)
    except KeyboardInterrupt:
        torch.save(net.state_dict(), 'INTERRUPTED.pth')
        logging.info('Saved interrupt')
        sys.exit(0)
