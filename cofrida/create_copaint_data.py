
##########################################################
#################### Copyright 2023 ######################
################ by Peter Schaldenbrand ##################
### The Robotics Institute, Carnegie Mellon University ###
################ All rights reserved. ####################
##########################################################

"""
Create a dataset to be used to fine-tune Stable Diffusion using LoRA
"""

import sys
import warnings 
sys.path.insert(0, '../src/')

import os 
import torch
import random
import numpy as np
import copy
import cv2
from datasets import load_dataset
from PIL import Image
import requests
from io import BytesIO
import kornia
from tqdm import tqdm
from torchvision import transforms
from torchvision.transforms import InterpolationMode 
bicubic = InterpolationMode.BICUBIC
from torchvision.utils import save_image

import matplotlib
import matplotlib.pyplot as plt
import pickle
import shutil

# Avoid annoying warning message. Doesn't slow down loading much.
os.environ["TOKENIZERS_PARALLELISM"] = "false"

# from plan import *
from paint_utils3 import format_img, initialize_painting, \
        discretize_colors, sort_brush_strokes_by_color, \
        add_strokes_to_painting, create_tensorboard, \
        load_img, get_colors
from clip_attn import get_attention
from painting_optimization import parse_objective
from options import Options

from transformers import CLIPProcessor, CLIPModel, CLIPTokenizer, CLIPTextModel
import torchvision.transforms as transforms


import clip 
if not os.path.exists('../src/clipscore/'):
    print('You have to clone the clipscore repo here from Github.')
sys.path.append('../src/clipscore')
from clipscore import get_clip_score, extract_all_images

# Load the CLIP model
device = "cuda" if torch.cuda.is_available() else "cpu"
model_ID = "openai/clip-vit-base-patch32"
model = CLIPModel.from_pretrained(model_ID).to(device)

preprocess = CLIPProcessor.from_pretrained(model_ID)
tokenizer = CLIPTokenizer.from_pretrained(model_ID)
text_encoder = CLIPTextModel.from_pretrained(model_ID).to(device)

# Define a function to load an image and preprocess it for CLIP
def load_image(image_path):
    response = requests.get(image_path, timeout=10)
    image = Image.open(BytesIO(response.content))

    return image

def image_text_similarity(image_path, text):

    with torch.no_grad():
        image = load_image(image_path)
        inputs = preprocess(text=[text], images=image, return_tensors="pt", padding=True)
        # [i.to(device) for i in inputs]
        inputs['input_ids'] = inputs['input_ids'].to(device)
        inputs['attention_mask'] = inputs['attention_mask'].to(device)
        inputs['pixel_values'] = inputs['pixel_values'].to(device)

        # inputs['input_ids'] = inputs['input_ids'].to(device)
        # print(inputs)
        outputs = model(**inputs)
        logits_per_image = outputs.logits_per_image  # this is the image-text similarity score
        probs = logits_per_image#logits_per_image.softmax(dim=1)  # we can take the softmax to get the label probabilities
    return probs

def image_text_similarity_local(pil_image, text):
    with torch.no_grad():
        inputs = preprocess(text=[text], images=pil_image, return_tensors="pt", padding=True)
        # [i.to(device) for i in inputs]
        inputs['input_ids'] = inputs['input_ids'].to(device)
        inputs['attention_mask'] = inputs['attention_mask'].to(device)
        inputs['pixel_values'] = inputs['pixel_values'].to(device)

        # inputs['input_ids'] = inputs['input_ids'].to(device)
        # print(inputs)
        outputs = model(**inputs)
        logits_per_image = outputs.logits_per_image  # this is the image-text similarity score
        probs = logits_per_image#logits_per_image.softmax(dim=1)  # we can take the softmax to get the label probabilities
    return probs

def plan_from_image(opt, num_strokes, target_img, current_canvas, clip_lr=1.0):
    global colors
    
    painting = initialize_painting(opt, 0, target_img, current_canvas, opt.ink)
    painting.to(device)

    c = 0
    painting = add_strokes_to_painting(opt, painting, painting(h,w)[:,:3], num_strokes, 
                                        target_img, current_canvas, opt.ink)
    painting.validate()
    optims = painting.get_optimizers(multiplier=opt.lr_multiplier, ink=opt.ink)

    # Learning rate scheduling. Start low, middle high, end low
    og_lrs = [o.param_groups[0]['lr'] if o is not None else None for o in optims]
    plans = []

    for it in tqdm(range(opt.n_iters), desc="Optim. {} Strokes".format(len(painting.brush_strokes))):
        for o in optims: o.zero_grad() if o is not None else None

        lr_factor = (1 - np.abs(it/opt.n_iters)) + 0.001 # 1.001 -> 0.001
        for i_o in range(len(optims)):
            if optims[i_o] is not None:
                optims[i_o].param_groups[0]['lr'] = og_lrs[i_o]*lr_factor

        p = painting(h, w, use_alpha=True, return_alphas=False)

        t = c / opt.n_iters
        c+=1 
        
        loss = 0
        # loss += parse_objective('l2', target_img, p[:,:3], weight=1-t)
        # loss += parse_objective('clip_conv_loss', target_img, p[:,:3], weight=clip_lr)
        # loss += parse_objective('l2', target_img, p[:,:3], weight=1)
        loss += parse_objective('clip_conv_loss', target_img, p[:,:3], weight=1)


        loss.backward()

        for o in optims: o.step() if o is not None else None
        painting.validate()

        if not opt.ink:
            painting = sort_brush_strokes_by_color(painting, bin_size=opt.bin_size)
        
        if (it % 10 == 0 and it > (0.5*opt.n_iters)) or it > 0.9*opt.n_iters:
            if not opt.ink:
                discretize_colors(painting, colors)

        # with torch.no_grad():
        #     p = p[:,:3]
        #     p = format_img(p)
        #     plans.append((p*255.).astype(np.uint8))

    # to_video(plans, fn=os.path.join(opt.plan_gif_dir,'controlnet_training{}.mp4'.format(str(time.time()))))
    # video_path = os.path.join(output_dir, 'id{}_{}strokes.jpg'.format(len(data_dict), opt.max_strokes_added))
    return painting

def process_pil(im, h=None, w=None):
    if im.mode != 'RGB':
        im = im.convert('RGB')
    im = np.array(im)
    # if im.shape[1] > max_size:
    #     fact = im.shape[1] / max_size
    im = cv2.resize(im, (w,h)) if h is not None and w is not None else im
    im = torch.from_numpy(im)
    im = im.permute(2,0,1)
    return im.unsqueeze(0).float() / 255.

def load_img_internet(url, h=None, w=None):
    try:
        response = requests.get(url, timeout=10)
        im = Image.open(BytesIO(response.content))
    except:
        return None
    # im = Image.open(path)
    return process_pil(im, h=h, w=w)


def load_lora_data_generator(lora_path=None):
    """
        Load a LoRA Stable Diffusion trained on past drawings/paintings
    """
    from diffusers import DiffusionPipeline
    pretrained_model_name_or_path = 'runwayml/stable-diffusion-v1-5'

    weight_dtype = torch.float16

    print('pretrained model', pretrained_model_name_or_path)

    # create pipeline
    pipeline = DiffusionPipeline.from_pretrained(
        pretrained_model_name_or_path, revision=None, torch_dtype=weight_dtype,
        safety_checker=None
    )
    pipeline = pipeline.to(device)
    pipeline.set_progress_bar_config(disable=True)

    # load attention processors
    if lora_path is not None: 
        print('Loading LoRA weights', lora_path)
        pipeline.unet.load_attn_procs(lora_path)
    return pipeline

def train_lora_data_generator(data_dict_fn, output_dir, pretrained_model="runwayml/stable-diffusion-v1-5"):
    """
        Train a LoRA of Stable Diffusion on past drawings/paintings for future training images
    """
    import subprocess
    args = [
        '--pretrained_model_name_or_path', pretrained_model,
        '--data_dict', data_dict_fn,
        '--dataloader_num_workers', '8',
        '--resolution', '512',
        '--center_crop', 
        '--random_flip',
        '--train_batch_size', '1',
        '--gradient_accumulation_steps', '4',
        '--learning_rate', '5e-05',
        '--max_grad_norm', '1',
        '--lr_scheduler', 'cosine',
        '--lr_warmup_steps', '0',
        '--output_dir', output_dir,
        '--report_to', 'tensorboard',
        '--validation_prompt', 'A frog astronaut.', 
            'The pittsburgh skyline', 
            'A drawing of the Pittsburgh skyline', 
            'A robot playing the piano', 
            'An avocado chair', 
            'Albert Einstein dancing"',
        '--validation_steps', '50',
        '--tracker_project_name', 'lora_create_data2',
        '--num_validation_images', '3',
        '--seed', '1337',
        # '--max_train_steps', '100',
        '--num_train_epochs', '1',
        '--resume_from_checkpoint', 'latest',
    ]
    process = subprocess.Popen(['accelerate', 'launch', '--mixed_precision=fp16', 'train_lora.py'] + args,
                        stdout=subprocess.PIPE, 
                        stderr=subprocess.PIPE)
    stdout, stderr = process.communicate()
    # print(stdout)
    print(str(stderr).encode('utf-8').decode('unicode_escape'))
    # print("the commandline is {}".format(process.args))


def get_image_text_pair(dataset):
    datums = []
    least_complicated_value = 1e9
    best_datum = None
    resize = transforms.Resize((256,256), bicubic, antialias=True)
    while len(datums) < opt.num_images_to_consider_for_simplicity:
        datum = dataset[np.random.randint(len(dataset))]
        img = load_img_internet(datum['URL'])
        if img is not None:
            datum['img'] = img
            mag, edges = kornia.filters.canny(resize(img))
            
            try:
                text_img_sim = image_text_similarity(datum['URL'], datum['TEXT'])
            except:
                continue
            # print(text_img_sim)
            if text_img_sim < 30: # Filter out images where the text caption isn't accurate
                continue
            
            datums.append(datum)

            if mag.sum() < least_complicated_value:
                least_complicated_value = mag.sum()
                best_datum = datum

    # all_imgs = torch.cat([resize(d['img']) for d in datums], dim=3)
    # all_imgs = torch.cat([all_imgs, resize(best_datum['img'])], dim=3)
    # show_img(all_imgs)

    # best_datum = dataset[np.random.randint(len(dataset))]
    # im = best_datum['image']
    # if im.mode != 'RGB':
    #     im = im.convert('RGB')
    # im = np.array(im)
    # # if im.shape[1] > max_size:
    # #     fact = im.shape[1] / max_size
    # im = cv2.resize(im, (w,h)) if h is not None and w is not None else im
    # im = torch.from_numpy(im)
    # im = im.permute(2,0,1)
    # best_datum['img'] = im.unsqueeze(0).float() / 255.

    return best_datum


def generate_image_text_pair(prompt, pipeline):
    with torch.no_grad():
        img = pipeline(
            prompt,
            num_inference_steps=30,
            num_images_per_prompt=1,
            output_type='pt'
        ).images[0]
        img = torch.clamp(img, min=0, max=1)
    return {
        'img':img.unsqueeze(0), # B, C, H, W 
        'TEXT':prompt
    }



def remove_strokes_randomly(painting, min_strokes_added, max_strokes_added):
    to_delete = set(random.sample(range(len(painting.brush_strokes)), max_strokes_added-min_strokes_added))
    enumerate(painting.brush_strokes)
    bs = [x for i,x in enumerate(painting.brush_strokes) if not i in to_delete]
    painting.brush_strokes = torch.nn.ModuleList(bs)
    # return painting
    with torch.no_grad():
        p = painting(h*4,w*4)
    return p[:,:3]


def remove_strokes_by_region(painting, target_img, prompt, keep_important=False):
    from clip_attn.clip_attn import get_attention
    attn = get_attention(target_img, prompt) 
    # attn = transforms.Resize((target_img.shape[2], target_img.shape[3]))(torch.from_numpy(attn[None,None,:,:]))[0,0]
    
    with torch.no_grad():
        p = painting(h*4,w*4, use_alpha=False)
    background = painting.background_img.detach().clone()
    output = p.detach().clone()

    background = transforms.Resize((h*4,w*4), bicubic, antialias=True)(background)
    attn = transforms.Resize((h*4,w*4), bicubic, antialias=True)(torch.from_numpy(attn[None,None,:,:]))[0,0]

    salient = attn > 0.25#torch.quantile(attn.float(), q=0.5)
    not_salient = ~salient
    
    if keep_important:
        for c in range(3):
            output[0,c][not_salient] = background[0,c][not_salient]
    else:
        for c in range(3):
            # print(output[0,c].shape, attn.shape)
            output[0,c][salient] = background[0,c][salient]
    return output, attn, salient

from segment_anything import sam_model_registry, SamAutomaticMaskGenerator

sam_checkpoint = "../src/sam_vit_b_01ec64.pth"
model_type = "vit_b"
try:
    sam = sam_model_registry[model_type](checkpoint=sam_checkpoint)
except Exception as e:
    print(e)
    print('try')
    print('wget https://dl.fbaipublicfiles.com/segment_anything/sam_vit_b_01ec64.pth')
sam.to(device=device)

mask_generator = SamAutomaticMaskGenerator(sam)

def remove_strokes_by_object(painting, target_img):
    # print(target_img.shape)
    target_img = transforms.Resize((h*4, w*4), bicubic, antialias=True)(target_img)
    with torch.no_grad():
        t = target_img[0].permute(1,2,0)
        t = (t.cpu().numpy()*255.).astype(np.uint8)
        # print(t.shape)
        masks = mask_generator.generate(t)
    
    def show_anns(anns):
        if len(anns) == 0:
            return
        sorted_anns = sorted(anns, key=(lambda x: x['area']), reverse=True)
        # ax = plt.gca()
        # ax.set_autoscale_on(False)

        img = np.ones((sorted_anns[0]['segmentation'].shape[0], sorted_anns[0]['segmentation'].shape[1], 4))
        img[:,:,3] = 0
        for ann in sorted_anns:
            m = ann['segmentation']
            color_mask = np.concatenate([np.random.random(3), [0.35]])
            img[m] = color_mask
        # ax.imshow(img)
        return img

    mask_img = show_anns(masks)
    # plt.imshow(show_anns(masks))
    # plt.show()
    # Big objects first
    # masks = sorted(masks, key=(lambda x: x['area']), reverse=True)
    random.shuffle(masks)
    # print(masks)
    # print(masks[0]['area'])
    # print(masks[0]['segmentation'].shape)
    # # plt.imshow(target_img)
    # masked = target_img.clone()
    # masked[0,:,masks[0]['segmentation']] = 1.0
    # plt.imshow(masked.cpu()[0].numpy().transpose(1,2,0))
    # plt.show()
    # matplotlib.use('TkAgg')
    # show_img(masked)
    # masks.area
    background = transforms.Resize((h*4, w*4), bicubic, antialias=True)(painting.background_img.detach().clone())
    boolean_mask = torch.zeros(background[:,:3].shape).to(device).float()
    with torch.no_grad():
        p = painting(h*4,w*4, use_alpha=False)
    if len(masks) > 0:
        # for i in range(min(max(1, int(len(masks)/2)), len(masks)-1)):
        for i in range(max(1, min(int(len(masks)/2), len(masks)-1))):
            p[0,:3,masks[i]['segmentation']] = background[0,:3,masks[i]['segmentation']]
            boolean_mask[0,:3,masks[i]['segmentation']] = 1
    return p[:,:3], mask_img, boolean_mask


clip_model, _ = clip.load("ViT-B/32", device=device, jit=False)
clip_model.eval()

def clip_score(text_fn, img_fn):
    image_paths = [img_fn]
    candidates = [text_fn]

    image_feats = extract_all_images(
        image_paths, clip_model, device, batch_size=64, num_workers=8)

    # get image-text clipscore
    with torch.no_grad():
        _, per_instance_image_text, candidate_feats = get_clip_score(
            clip_model, image_feats, candidates, device)

    return per_instance_image_text[0]

if __name__ == '__main__':
    global opt, h, w
    opt = Options()
    opt.gather_options()

    os.makedirs(opt.output_parent_dir, exist_ok=True)

    data_dict_fn = os.path.join(opt.output_parent_dir, 'data_dict.pkl')

    opt.writer = create_tensorboard(log_dir=opt.tensorboard_dir)

    w = int(opt.render_height * (opt.CANVAS_WIDTH_M/opt.CANVAS_HEIGHT_M))
    h = int(opt.render_height)

    # Get the background of painting to be the current canvas
    if os.path.exists(opt.cofrida_background_image):
        current_canvas = load_img(opt.cofrida_background_image, h=h, w=w).to(device)/255.
    else:
        current_canvas = torch.ones(1,3,h,w).to(device)
    default_current_canvas = copy.deepcopy(current_canvas)

    dataset = load_dataset(opt.cofrida_dataset)['train']

    if opt.generate_cofrida_training_data:
        prompts = []
        for p in dataset:
            if p['Challenge'] in ['Basic', 'Simple Detail', 'Fine-Grained Detail']:
                prompts.append(p['Prompt'])
    
    crop = transforms.RandomResizedCrop((h*4, w*4), scale=(0.7, 1.0), 
                                        ratio=(0.95,1.05), antialias=True)
    
    bg_aug = transforms.Compose([
        transforms.RandomResizedCrop((h, w), scale=(0.7, 1.0), 
                                        ratio=(0.75,1.0), antialias=True),
        transforms.ColorJitter(brightness=(0.5, 1.25), hue=0.2, contrast=0.1, saturation=0.2)
    ])

    data_dict = []
    if os.path.exists(data_dict_fn):
        data_dict = pickle.load(open(data_dict_fn,'rb'))
    
    painting = None

    if opt.generate_cofrida_training_data:
        lora_model_dir = os.path.join(opt.output_parent_dir, 'lora_model')
        os.makedirs(lora_model_dir, exist_ok=True)
        lora_pipeline = load_lora_data_generator(
            lora_path=lora_model_dir if os.path.exists(os.path.join(lora_model_dir, 'pytorch_lora_weights.bin')) else None)

    for i in range(opt.max_images):
        if opt.generate_cofrida_training_data:
            # Update the LoRA model that generates the images to paint
            if ((i+1)%opt.retrain_cofrida_image_generator) == 0 and (len(data_dict) > 100):
                print('Training LoRA model on previously made drawings.')
                del painting, lora_pipeline # Free up memory
                train_lora_data_generator(data_dict_fn=data_dict_fn, 
                                        output_dir=lora_model_dir)
                lora_pipeline = load_lora_data_generator(lora_path=lora_model_dir)


        # Get a new image
        try:
            if opt.generate_cofrida_training_data:
                datum = generate_image_text_pair(prompts[random.randint(0,len(prompts))], lora_pipeline)
            else:
                datum = get_image_text_pair(dataset)
        except Exception as e:
            print(e)
            continue
        target_img_full = crop(datum['img']).to(device)
        target_img = transforms.Resize((h,w), bicubic, antialias=True)(target_img_full)
        
        if opt.colors is not None:
            # 209,0,0.241,212,69.39,94,195
            # 235,137,15 orange
            # 115,66,16 brown
            # 138,99,139 purple
            colors = np.array([i.split(',') for i in opt.colors.split('.')]).astype(np.float32)
            colors = (torch.from_numpy(colors) / 255.).to(device)
        else:
            colors = get_colors(cv2.resize((target_img.cpu().numpy()[0].transpose(1,2,0)*255.).astype(np.uint8), (256, 256)), 
                n_colors=opt.n_colors).to(device)

        datum_no_img = copy.deepcopy(datum)
        datum_no_img['img'] = None # Don't save the image directly, just path
        current_canvas = bg_aug(default_current_canvas)

        full_painting_strokes = random.randint(opt.min_strokes_added, opt.max_strokes_added)
    
        painting = plan_from_image(opt, full_painting_strokes, target_img, current_canvas[:,:3])

        # If the painting doesn't fit the text prompt well, just break
        with torch.no_grad():
            final_painting = painting(h*4,w*4)
        output_dir = os.path.join(opt.output_parent_dir, str(int(np.floor(len(data_dict)/100))),)
        output_rel_dir = os.path.join(str(int(np.floor(len(data_dict)/100))),)
        if not os.path.exists(output_dir): os.mkdir(output_dir)
        final_img_path = os.path.join(output_dir, 'id{}_{}strokes.png'.format(len(data_dict), full_painting_strokes))
        save_image(final_painting[:,:3],final_img_path)
        with warnings.catch_warnings(): # suppress annoing clip_score warning
            warnings.simplefilter("ignore")
            cs = clip_score(datum['TEXT'], final_img_path)
        # print('clip score:', cs, datum['TEXT'])

        target_img_already_saved = False
        for method in ['random', 'random', 'random', 'random', 
                       'salience', 'not_salience', 'object', 'object', 'all']:
            try:
                # Make sub-directories so single directories don't get too big
                output_dir = os.path.join(opt.output_parent_dir,
                                        str(int(np.floor(len(data_dict)/100))),)
                output_rel_dir = os.path.join(str(int(np.floor(len(data_dict)/100))))
                if not os.path.exists(output_dir): os.mkdir(output_dir)

                # Save the target image once per stroke removal method (avoid large memory usage)
                if not target_img_already_saved:
                    target_img_path = os.path.join(output_dir, 'id{}_target.png'.format(len(data_dict)))
                    target_img_rel_path = os.path.join(output_rel_dir, 'id{}_target.png'.format(len(data_dict)))
                    save_image(target_img_full[:,:3],target_img_path)
                    target_img_already_saved = True
                            
                with torch.no_grad():
                    final_painting = painting(h*4,w*4)

                final_img_path = os.path.join(output_dir, 'id{}_{}strokes.png'.format(len(data_dict), full_painting_strokes))
                final_img_rel_path = os.path.join(output_rel_dir, 'id{}_{}strokes.png'.format(len(data_dict), full_painting_strokes))
                save_image(final_painting[:,:3],final_img_path)

                # How many strokes in the random removal canvas
                partial_painting_strokes = random.randint(int(0.25*full_painting_strokes), int(0.75*full_painting_strokes))

                if method == 'random':
                    # Randomly remove strokes to get the start image
                    start_painting = remove_strokes_randomly(copy.deepcopy(painting), 
                                                            partial_painting_strokes, full_painting_strokes)
                elif method == 'salience':
                    # Remove strokes by region
                    start_painting, attn, salient = remove_strokes_by_region(copy.deepcopy(painting), 
                                                            target_img, datum["TEXT"])
                    attn_path = os.path.join(output_dir, 'id{}_{}_attn.png'.format(len(data_dict), 
                                                                            full_painting_strokes))
                    save_image(attn[None,None].float().repeat((1,3,1,1)), attn_path)
                    salience_path = os.path.join(output_dir, 'id{}_{}_salience.png'.format(len(data_dict), 
                                                                            full_painting_strokes))
                    save_image(salient[None,None].float().repeat((1,3,1,1)), salience_path)
                    
                elif method == 'not_salience':
                    # Remove strokes by region
                    start_painting, attn, salient = remove_strokes_by_region(copy.deepcopy(painting), 
                                                            target_img, datum["TEXT"], keep_important=True)
                elif method == 'object':
                    start_painting, mask_img, boolean_mask = remove_strokes_by_object(copy.deepcopy(painting), 
                                                            target_img)
                    mask_path = os.path.join(output_dir, 'id{}_{}_mask.png'.format(len(data_dict), 
                                                                            full_painting_strokes))
                    Image.fromarray((mask_img*254).astype(np.uint8)).save(mask_path)
                    boolean_mask_path = os.path.join(output_dir, 'id{}_{}_bool_obj_mask.png'.format(len(data_dict), 
                                                                            full_painting_strokes))
                    boolean_mask = boolean_mask[0].cpu().numpy().transpose(1,2,0)
                    Image.fromarray((boolean_mask*254).astype(np.uint8)).save(boolean_mask_path)
                elif method == 'all':
                    start_painting = painting.background_img
                else:
                    print("Not sure which removal method you mean")
                    1/0

                # Don't save if the start painting is too similar to final painting
                diff = torch.mean(torch.abs(transforms.Resize((256,256), antialias=True)(start_painting[:,:3]) \
                            - transforms.Resize((256,256), antialias=True)(final_painting[:,:3])))
                # print(diff)
                # if diff < 0.025:
                #     # print('not different enough')
                #     continue

                start_img_path = os.path.join(output_dir, 'id{}_start.png'.format(len(data_dict)))
                start_img_rel_path = os.path.join(output_rel_dir, 'id{}_start.png'.format(len(data_dict)))
                save_image(start_painting[:,:3],start_img_path)

                d = {'id':len(data_dict),
                        'num_strokes_added':full_painting_strokes-partial_painting_strokes,
                        'num_prev_strokes':partial_painting_strokes,
                        'start_img':start_img_rel_path,
                        'final_img':final_img_rel_path,
                        'target_img':target_img_rel_path,
                        'method':method,
                    #  'text':datum['text'],#sketches
                        'text':datum['TEXT'],
                        'photo_to_sketch_diff': diff.item(),
                        'clip_score':cs,
                        'dataset_info':datum_no_img}

                # current_canvas = p.detach()
                # start_img_path = final_img_path

                current_canvas = final_painting.detach()
                current_canvas = transforms.Resize((h,w), antialias=True)(current_canvas)

                data_dict.append(d)

                if os.path.exists(data_dict_fn):
                    shutil.copyfile(data_dict_fn,
                                os.path.join(opt.output_parent_dir, 'data_dict_saved.pkl'))
                                    
                with open(data_dict_fn,'wb') as f:
                    pickle.dump(data_dict, f)
            except Exception as e:
                print('Exception', e)
                continue
        
