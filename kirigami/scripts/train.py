import os
import json
import argparse
import multipledispatch
from tqdm import tqdm
import munch
import torch
from torch import nn
from torch.utils.data import DataLoader
from utils.utils import path2munch
from utils.data_utils import *
from nn.SPOT import *
from nn.Embedding import *
from nn.MainNet import *


@dispatch(argparse.Namespace)
def train(args) -> None:
    config = path2munch(args.config)
    return train(config)


@dispatch(Munch)
def train(conf) -> None:
    start_epoch = 0
    model = MainNet(conf.model)
    loss_func = getattr(nn, conf.loss_func.class_name)(**conf.loss_func.params)
    optimizer = getattr(torch.optim, conf.optim.class_name)(model.parameters(),
                                                            **conf.optim.params)

    dataset_class = TensorDataset if conf.data.pre_embed else BpseqDataset
    train_set = dataset_class(conf.data.training_list)
    train_loader = DataLoader(train_set,
                              batch_size=conf.data.batch_size,
                              shuffle=conf.data.shuffle)
    train_loop = tqdm(train_loader) if conf.training.show_bar else train_loader

    if conf.data.validation_list:
        val_set = dataset_class(conf.data.validation_list)
        val_loader = DataLoader(val_set,
                                batch_size=conf.data.batch_size,
                                shuffle=conf.data.shuffle)
        val_loop = tqdm(val_loader) if conf.training.show_bar else val_loader

    if conf.resume:
        assert os.path.exists(conf.training.checkpoint), "Cannot find checkpoint file"
        checkpoint = torch.load(conf.training.checkpoint)
        model.load_state_dict(checkpoint['model_state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        start_epoch = checkpoint['epoch']
        loss = checkpoint['loss']

    best_val_loss = float('inf')

    for epoch in range(start_epoch, conf.training.epochs):
        train_loss_tot = 0.
        for seq, lab in train_loop:
            pred = model(seq)
            loss = loss_func(pred, lab)
            train_loss_tot += loss
            loss.backward()
            optimizer.step()
            optimizer.zero_grad()
        train_loss_mean = train_loss_tot / len(train_loader)
        torch.save({'epoch': epoch,
                    'model_state_dict': model.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    'loss': train_loss_mean},
                   conf.training.checkpoint)

        if conf.data.validation_list:
            val_loss_tot = 0.
            for seq, lab in val_loop:
                pred = model(seq)
                loss = loss_func(pred, lab)
                val_loss_tot += loss
                val_loss_mean = val_loss_tot / len(val_loader)
            if val_loss_mean < best_val_loss:
                best_val_loss = val_loss_mean
                torch.save({'epoch': epoch,
                            'model_state_dict': model.state_dict(),
                            'optimizer_state_dict': optimizer.state_dict(),
                            'loss': best_val_loss},
                           conf.training.best)

        if epoch % conf.training.print_frequency == 0:
            print(f'Mean training loss for epoch {epoch}: {train_loss_mean}')
            if conf.data.validation_list:
                print(f'Mean validation loss for epoch {epoch}: {val_loss_mean}')
            print()
