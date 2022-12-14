import cv2
import gc
import time
import numpy as np
import pandas as pd
import itertools
from tqdm.autonotebook import tqdm
import albumentations as A
import matplotlib.pyplot as plt

import torch
from torch import nn
import torch.nn.functional as F
import timm
from transformers import DistilBertModel, DistilBertConfig, DistilBertTokenizer


df = pd.read_csv("D:/_AIA_Team_Project_Data/Image_Captioning/_data/Flickr8k/captions.txt") # txt파일을 csv형식으로 불러온다 , 기준으로 colum 나눔
df['id'] = [id_ for id_ in range(df.shape[0] // 5) for _ in range(5)]
df.to_csv("D:/_AIA_Team_Project_Data/Image_Captioning/_data/Flickr8k/captions.csv", index=False)
df = pd.read_csv("D:/_AIA_Team_Project_Data/Image_Captioning/_data/Flickr8k/captions.csv")
image_path = "D:/_AIA_Team_Project_Data/Image_Captioning/_data/Flickr8k/Images"
captions_path = "D:/_AIA_Team_Project_Data/Image_Captioning/_data/Flickr8k/"
    

print(df.head())

class CFG:
    debug = False
    image_path = image_path
    captions_path = captions_path
    batch_size = 32
    num_workers = 0
    head_lr = 1e-3
    image_encoder_lr = 1e-4
    text_encoder_lr = 1e-5
    weight_decay = 1e-3
    patience = 1
    factor = 0.8
    epochs = 5
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    model_name = 'resnet50'
    image_embedding = 2048
    text_encoder_model = "distilbert-base-uncased" # uncased: 대소문자 구별x, MLM
    text_embedding = 768
    text_tokenizer = "distilbert-base-uncased"
    max_length = 200

    pretrained = True # for both image encoder and text encoder
    trainable = True # for both image encoder and text encoder
    temperature = 1.0

    # image size
    size = 224

    # for projection head; used for both image and text encoders
    num_projection_layers = 1
    projection_dim = 256 
    dropout = 0.1
    
class AvgMeter:
    def __init__(self, name="Metric"):
        self.name = name
        self.reset()

    def reset(self):
        self.avg, self.sum, self.count = [0] * 3

    def update(self, val, count=1):
        self.count += count
        self.sum += val * count # loss / 32
        self.avg = self.sum / self.count

    def __repr__(self):
        text = f"{self.name}: {self.avg:.4f}"
        return text

def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group["lr"]

class CLIPDataset(torch.utils.data.Dataset): 
    def __init__(self, image_filenames, captions, tokenizer, transforms):
                     # dataframe["image"].values, dataframe["caption"].values, tokenizer=tokenizer, transforms=transforms,
        """
        image_filenames and cpations must have the same length; so, if there are
        multiple captions for each image, the image_filenames must have repetitive
        file names 
        """
        self.image_filenames = image_filenames
        self.captions = list(captions)
        self.encoded_captions = tokenizer(list(captions), padding=True, truncation=True, max_length=CFG.max_length)      
        self.transforms = transforms                                  # truncation : padding과 반대로 긴 sequence들을 자른다
                                                                      # max_length가 주어진 경우 그 길이에 맞춰 자른다 
                                                                      # 아닌 경우 model의 가능한 최대 input 길이에 맞춘다 
    def __getitem__(self, idx):
        item = {key: torch.tensor(values[idx]) for key, values in self.encoded_captions.items()}
                # key : 토큰된 문장, values : attention_mask

        image = cv2.imread(f"{CFG.image_path}/{self.image_filenames[idx]}") # 이미지 index 경로
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image = self.transforms(image=image)['image']
        item['image'] = torch.tensor(image).permute(2, 0, 1).float()
        item['caption'] = self.captions[idx]

        return item


    def __len__(self):
        return len(self.captions)



def get_transforms(mode="train"): # transforms - Normalize
    if mode == "train":
        return A.Compose(
            [
                A.Resize(CFG.size, CFG.size, always_apply=True),
                A.Normalize(max_pixel_value=255.0, always_apply=True),
            ]
        )
    else:
        return A.Compose(
            [
                A.Resize(CFG.size, CFG.size, always_apply=True),
                A.Normalize(max_pixel_value=255.0, always_apply=True),
            ]
        )
        
class ImageEncoder(nn.Module):
    """
    Encode images to a fixed size vector
    """

    def __init__(self, model_name=CFG.model_name, pretrained=CFG.pretrained, trainable=CFG.trainable) : #  resnet50, True, True
        super().__init__()
        self.model = timm.create_model(model_name, pretrained, num_classes=0, global_pool="avg")  # num_classes : output 개수
        for p in self.model.parameters():
            p.requires_grad = trainable # trainable = True -> 연산들의 추적 시작(역전파 시작)

    def forward(self, x):
        return self.model(x)
    
    
class TextEncoder(nn.Module):
    def __init__(self, model_name=CFG.text_encoder_model, pretrained=CFG.pretrained, trainable=CFG.trainable):
        super().__init__()          # text_encoder_model = "distilbert-base-uncased"
        if pretrained:
            self.model = DistilBertModel.from_pretrained(model_name) # distilbert-base-uncased -> distilbert의 토크나이저
        else:
            self.model = DistilBertModel(config=DistilBertConfig()) # DistilBertConfig 구성으로 설정
            
        for p in self.model.parameters():
            p.requires_grad = trainable

        # we are using the CLS token hidden representation as the sentence's embedding
        self.target_token_idx = 0

    def forward(self, input_ids, attention_mask) : 
        output = self.model(input_ids=input_ids, attention_mask=attention_mask)
        last_hidden_state = output.last_hidden_state
        return last_hidden_state[:, self.target_token_idx, :]
    
class ProjectionHead(nn.Module): # 차원 맞춰주려고 한다 transformer의 fc_out
    def __init__(
        self,
        embedding_dim,
        projection_dim=CFG.projection_dim,
        dropout=CFG.dropout
    ):
        super().__init__()
        self.projection = nn.Linear(embedding_dim, projection_dim)
        self.gelu = nn.GELU()
        self.fc = nn.Linear(projection_dim, projection_dim)
        self.dropout = nn.Dropout(dropout)
        self.layer_norm = nn.LayerNorm(projection_dim)
    
    def forward(self, x):
        projected = self.projection(x)
        x = self.gelu(projected)
        x = self.fc(x)
        x = self.dropout(x)
        x = x + projected
        x = self.layer_norm(x)
        return x
    
class CLIPModel(nn.Module):
    def __init__(
        self,
        temperature=CFG.temperature,
        image_embedding=CFG.image_embedding,
        text_embedding=CFG.text_embedding,
    ):
        super().__init__()
        self.image_encoder = ImageEncoder()
        self.text_encoder = TextEncoder()
        self.image_projection = ProjectionHead(embedding_dim=image_embedding)
        self.text_projection = ProjectionHead(embedding_dim=text_embedding)
        self.temperature = temperature

    def forward(self, batch):
        # Getting Image and Text Features
        image_features = self.image_encoder(batch["image"])
        text_features = self.text_encoder(
            input_ids=batch["input_ids"], attention_mask=batch["attention_mask"]
        )
        # Getting Image and Text Embeddings (with same dimension)
        image_embeddings = self.image_projection(image_features)
        text_embeddings = self.text_projection(text_features)

        # Calculating the Loss
        logits = (text_embeddings @ image_embeddings.T) / self.temperature  # 템퍼쳐 값이 커질 수록 logits 값이 줄어듦. 기준이 빡세짐. 얘는 로그소프트맥스 취하고
        images_similarity = image_embeddings @ image_embeddings.T           # 어텐션 에너지값은 그냥 소프트맥스 취해서 로스를 구함
        texts_similarity = text_embeddings @ text_embeddings.T
        # 텍스트 피쳐와 이미지 피쳐를 행렬곱해서 트포처럼 어텐션 에너지값을 구함
        targets = F.softmax(
            (images_similarity + texts_similarity) / 2 * self.temperature, dim=-1
        )
        texts_loss = cross_entropy(logits, targets, reduction='none')
        images_loss = cross_entropy(logits.T, targets, reduction='none')
        loss =  (images_loss + texts_loss) / 2.0 # shape: (batch_size)
        return loss.mean()


def cross_entropy(preds, targets, reduction='none'):
    log_softmax = nn.LogSoftmax(dim=-1)
    loss = (-targets * log_softmax(preds)).sum(1)   # targets.shape = (32,32)   preds.shape = (32,32)
    if reduction == "none":
        return loss
    elif reduction == "mean":
        return loss.mean()
    
# A simple Example

batch_size = 4
dim = 256
embeddings = torch.randn(batch_size, dim)
out = embeddings @ embeddings.T
print(F.softmax(out, dim=-1))

def make_train_valid_dfs() : # 데이터 전처리 
    dataframe = pd.read_csv(f"{CFG.captions_path}/captions.csv")
    max_id = dataframe["id"].max() + 1 if not CFG.debug else 100
    image_ids = np.arange(0, max_id)
    np.random.seed(42)
    valid_ids = np.random.choice(image_ids, size=int(0.2 * len(image_ids)), replace=False)
    train_ids = [id_ for id_ in image_ids if id_ not in valid_ids]
    train_dataframe = dataframe[dataframe["id"].isin(train_ids)].reset_index(drop=True)
    print('dataF:', train_dataframe.head())
    valid_dataframe = dataframe[dataframe["id"].isin(valid_ids)].reset_index(drop=True)
    return train_dataframe, valid_dataframe

#                            image                                            caption    id
# 0      1000268201_693b08cb0e.jpg  A child in a pink dress is climbing up a set o...     0
# 1      1000268201_693b08cb0e.jpg              A girl going into a wooden building .     0
# 2      1000268201_693b08cb0e.jpg   A little girl climbing into a wooden playhouse .     0
# 3      1000268201_693b08cb0e.jpg  A little girl climbing the stairs to her playh...     0
# 4      1000268201_693b08cb0e.jpg  A little girl in a pink dress going into a woo...     0        



def build_loaders(dataframe, tokenizer, mode):
    transforms = get_transforms(mode=mode)
    dataset = CLIPDataset(
        dataframe["image"].values,
        dataframe["caption"].values,
        tokenizer=tokenizer,
        transforms=transforms,
    )
    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=CFG.batch_size,
        num_workers=CFG.num_workers,
        shuffle=True if mode == "train" else False,
    )
    return dataloader

def train_epoch(model, train_loader, optimizer, lr_scheduler, step):
    loss_meter = AvgMeter()
    tqdm_object = tqdm(train_loader, total=len(train_loader))
    for batch in tqdm_object:
        batch = {k: v.to(CFG.device) for k, v in batch.items() if k != "caption"}
        loss = model(batch)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        if step == "batch":
            lr_scheduler.step()

        count = batch["image"].size(0)
        loss_meter.update(loss.item(), count)

        tqdm_object.set_postfix(train_loss=loss_meter.avg, lr=get_lr(optimizer))
    return loss_meter


def valid_epoch(model, valid_loader):
    loss_meter = AvgMeter()

    tqdm_object = tqdm(valid_loader, total=len(valid_loader))
    for batch in tqdm_object:
        batch = {k: v.to(CFG.device) for k, v in batch.items() if k != "caption"}
        loss = model(batch)

        count = batch["image"].size(0)
        loss_meter.update(loss.item(), count)

        tqdm_object.set_postfix(valid_loss=loss_meter.avg)
    return loss_meter


def main():
    train_df, valid_df = make_train_valid_dfs()                         # 데이터셋 전처리
    tokenizer = DistilBertTokenizer.from_pretrained(CFG.text_tokenizer) # DistilBertTokenizer의 전이학습된 토크나이저 사용
    train_loader = build_loaders(train_df, tokenizer, mode="train")     # 데이터를 학습할 수 있도록 만들기
    valid_loader = build_loaders(valid_df, tokenizer, mode="valid")     
    
    model = CLIPModel().to(CFG.device)
    
    params = [                                                                      # 파라미터 선언                                            
        {"params": model.image_encoder.parameters(), "lr": CFG.image_encoder_lr},   # lr : 1e-4
        {"params": model.text_encoder.parameters(), "lr": CFG.text_encoder_lr},     # lr : 1e-5
        {"params": itertools.chain(                                                 # itertools.chain = 인자값들을 합쳐서 순차적으로 진행
            model.image_projection.parameters(), model.text_projection.parameters()
        ), "lr": CFG.head_lr, "weight_decay": CFG.weight_decay}                     # lr : 1e-3, weight_decay : 1e-3
    ]
    optimizer = torch.optim.AdamW(params, weight_decay=CFG.weight_decay)             
    lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(           # metric의 감소가 멈출 때 learning rate를 감소시키는 scheduler
        optimizer,
        mode="min",
        patience=CFG.patience, # patience : metric이 얼마 동안 변화가 없을 때 learning rate를 감소시킬지 결정 patience : 1
        factor=CFG.factor      # factor   : Learning rate를 감소시키는 비율. new_lr = lr * factor           factor : 0.8
    )


    step = "epoch"
    start_time = time.time()
    best_loss = float('inf')
    
    for epoch in range(CFG.epochs) : # train
        print(f"Epoch: {epoch + 1}") 
        model.train() 
        train_loss = train_epoch(model, train_loader, optimizer, lr_scheduler, step) 
        model.eval()
        with torch.no_grad():
            valid_loss = valid_epoch(model, valid_loader)
        
        if valid_loss.avg < best_loss:
            best_loss = valid_loss.avg
            torch.save(model.state_dict(), "D:\_AIA_Team_Project_Data\CLIP/Clip_epoch_5.pt")   # ------------------ Model Save
            print("Saved Best Model!")
        
        lr_scheduler.step(valid_loss.avg)
    end_time = time.time() - start_time
    return end_time
        
end_time = main()

print('took', round(end_time), 'sec.')
print(f'epochs: {CFG.epochs}    batch size: {CFG.batch_size}')

def get_image_embeddings(valid_df, model_path):
    tokenizer = DistilBertTokenizer.from_pretrained(CFG.text_tokenizer)
    valid_loader = build_loaders(valid_df, tokenizer, mode="valid")
    
    model = CLIPModel().to(CFG.device)
    model.load_state_dict(torch.load(model_path, map_location=CFG.device))
    model.eval()
    
    valid_image_embeddings = []
    with torch.no_grad():
        for batch in tqdm(valid_loader):
            image_features = model.image_encoder(batch["image"].to(CFG.device))
            image_embeddings = model.image_projection(image_features)
            valid_image_embeddings.append(image_embeddings)
    return model, torch.cat(valid_image_embeddings)

_, valid_df = make_train_valid_dfs()
model, image_embeddings = get_image_embeddings(valid_df, "D:\_AIA_Team_Project_Data\CLIP/Clip_epoch_5.pt")

def find_matches(model, image_embeddings, query, image_filenames, n=9):
    tokenizer = DistilBertTokenizer.from_pretrained(CFG.text_tokenizer)
    encoded_query = tokenizer([query])
    batch = {
        key: torch.tensor(values).to(CFG.device)
        for key, values in encoded_query.items()
    }
    with torch.no_grad():
        text_features = model.text_encoder(
            input_ids=batch["input_ids"], attention_mask=batch["attention_mask"]
        )
        text_embeddings = model.text_projection(text_features)
    
    image_embeddings_n = F.normalize(image_embeddings, p=2, dim=-1)
    text_embeddings_n = F.normalize(text_embeddings, p=2, dim=-1)
    dot_similarity = text_embeddings_n @ image_embeddings_n.T
    
    values, indices = torch.topk(dot_similarity.squeeze(0), n * 5) # argmax 인데 제일 큰거부터 (n=)9*5개 인덱스 반환함
    print('indices', indices)
    matches = [image_filenames[idx] for idx in indices[::5]]
    
    _, axes = plt.subplots(3, 3, figsize=(10, 10))
    for match, ax in zip(matches, axes.flatten()):
        image = cv2.imread(f"{CFG.image_path}/{match}")
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        ax.imshow(image)
        ax.axis("off")
    
    plt.show()
    
find_matches(model, image_embeddings, query="people in th snow", image_filenames=valid_df['image'].values, n=9)
                                                                # valid 데이터셋의 이미지들 중에서 텍스트와 매치되는 놈을 보여줌
                                                                
# took 2246 sec.
# epochs: 5    batch size: 32

# 구조를 쉽게 요약하면 어텐션 기법을 사용하여 이미지 피처와 텍스트 피처의 유사도를 계속 구하는 방식으로 훈련하고
# 예측의 경우 텍스트 피처를 넣으면 이미지 피처를 클래시파이어 클래스로 두고
# 그 중에서 topk - 5 의 방식으로 5장을 해당 텍스트에 관련된 이미지라고 예측