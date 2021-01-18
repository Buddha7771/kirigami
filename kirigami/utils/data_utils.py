import torch
from torch.utils.data import Dataset, DataLoader
from ..nn.Embedding import *


class AbstractDataset(Dataset):
    def __init__(self, list_file: str, embed):
        super(AbstractDataset, self).__init__()
        self.embed = embed
        with open(list_file, 'r') as f:
            self.files = f.read.splitlines()

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        file = self.files[idx]
        with open(file, 'r') as f:
            file_str = f.read()
        return self.embed(file_str)


class FastaDataset(AbstractDataset):
    def __init__(self, list_file: str):
        super(FastaDataset, self).__init__(list_file)
        self.embed = SequenceEmbedding()


class LabelDataset(AbstractDataset):
    def __init__(self, list_file: str):
        super(FastaDataset, self).__init__(list_file)
        self.embed = LabelEmbedding()


class BpseqDataset(Dataset):
    def __init__(self, list_file: str):
        super(FBpseqDataset, self).__init__(list_file)
        self.embed = BpseqEmbedding()