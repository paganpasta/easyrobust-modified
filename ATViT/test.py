import argparse
import datetime
import json
import logging
import os
import random

import torch
from timm.models import create_model
from torch.utils.data import DataLoader
from torchvision import transforms, models, utils as vutils
from tqdm import tqdm

import vit_models
from attack import normalize, local_adv
from dataset import AdvImageNet

targeted_class_dict = {
    24: "Great Grey Owl",
    99: "Goose",
    245: "French Bulldog",
    344: "Hippopotamus",
    471: "Cannon",
    555: "Fire Engine",
    661: "Model T",
    701: "Parachute",
    802: "Snowmobile",
    919: "Street Sign ",
}


def parse_args():
    parser = argparse.ArgumentParser(description='Transformers')
    parser.add_argument('--test_dir', default='data', help='ImageNet Validation Data')
    parser.add_argument('--dataset', default="imagenet_5k", help='dataset name')
    parser.add_argument('--src_model', type=str, default='deit_small_patch16_224', help='Source Model Name')
    parser.add_argument('--tar_model', type=str, default='T2t_vit_24', help='Target Model Name')
    parser.add_argument('--src_pretrained', type=str, default=None, help='pretrained path for source model')
    parser.add_argument('--scale_size', type=int, default=256, help='')
    parser.add_argument('--img_size', type=int, default=224, help='')
    parser.add_argument('--batch_size', type=int, default=1, help='Batch Size')
    parser.add_argument('--eps', type=int, default=8, help='Perturbation Budget')
    parser.add_argument('--iter', type=int, default=10, help='Attack iterations')
    parser.add_argument('--index', type=str, default='last', help='last or all')
    parser.add_argument('--attack_type', type=str, default='fgsm', help='fgsm, mifgsm, dim, pgd')
    parser.add_argument('--tar_ensemble', action="store_true", default=False)
    parser.add_argument('--apply_ti', action="store_true", default=False)
    parser.add_argument('--save_im', action="store_true", default=False)
    parser.add_argument('--ckpt-path', default=None, type=str)
    return parser.parse_args()


def get_model(model_name):
    model_names = sorted(name for name in models.__dict__
                         if name.islower() and not name.startswith("__")
                         and callable(models.__dict__[name]))
    other_model_names = vars(vit_models)

    # get the source model
    if model_name in model_names:
        model = models.__dict__[model_name](pretrained=True)
        mean = (0.485, 0.456, 0.406)
        std = (0.229, 0.224, 0.225)
    elif 'deit' in model_name:
        model = create_model(model_name, pretrained=True)
        mean = (0.485, 0.456, 0.406)
        std = (0.229, 0.224, 0.225)
    elif 'hierarchical' in model_name or "ensemble" in model_name:
        model = create_model(model_name, pretrained=True)
        mean = (0.485, 0.456, 0.406)
        std = (0.229, 0.224, 0.225)
    elif 'vit' in model_name:
        model = create_model(model_name, pretrained=True)
        mean = (0.485, 0.456, 0.406)
        std = (0.229, 0.224, 0.225)
        #mean = (0.5, 0.5, 0.5)
        #std = (0.5, 0.5, 0.5)
    elif 'T2t' in model_name:
        model = create_model(model_name, pretrained=True)
        mean = (0.485, 0.456, 0.406)
        std = (0.229, 0.224, 0.225)
    elif 'tnt' in model_name:
        model = create_model(model_name, pretrained=True)
        mean = (0.5, 0.5, 0.5)
        std = (0.5, 0.5, 0.5)
    elif 'swin' in model_name:
        model = create_model(model_name, pretrained=True)
        mean = (0.485, 0.456, 0.406)
        std = (0.229, 0.224, 0.225)
    else:
        raise NotImplementedError(f"Please provide correct model names: {model_names}")
    return model, mean, std


#  Test Samples
def get_data_loader(args, verbose=True):
    data_transform = transforms.Compose([
        transforms.Resize(args.scale_size),
        transforms.CenterCrop(args.img_size),
        transforms.ToTensor(),
    ])

    test_dir = args.test_dir
    if args.dataset == "imagenet_1k":
        test_set = AdvImageNet(image_list="data/image_list_1k.json", root=test_dir, transform=data_transform)
    else:
        test_set = AdvImageNet(root=test_dir, transform=data_transform)
    test_size = len(test_set)
    if verbose:
        print('Test data size:', test_size)
    test_loader = torch.utils.data.DataLoader(test_set, batch_size=args.batch_size, shuffle=True, num_workers=4,
                                              pin_memory=True)
    return test_loader, test_size


def main():
    # setup run
    args = parse_args()
    args.exp = f"{datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}_{random.randint(1, 100)}"
    os.makedirs(f"report/{args.exp}")
    json.dump(vars(args), open(f"report/{args.exp}/config.json", "w"), indent=4)

    logger = logging.getLogger(__name__)
    logfile = f'report/{args.exp}/{args.src_model}_5k_val_eps_{args.eps}.log'
    logging.basicConfig(
        format='[%(asctime)s] - %(message)s',
        datefmt='%Y/%m/%d %H:%M:%S',
        level=logging.INFO,
        filename=logfile)

    # GPU
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

    # load source and target models
    src_model, src_mean, src_std = get_model(args.src_model)
    if args.src_pretrained is not None:
        if args.src_pretrained.startswith("https://"):
            src_checkpoint = torch.hub.load_state_dict_from_url(args.src_pretrained, map_location='cpu')
        else:
            src_checkpoint = torch.load(args.src_pretrained, map_location='cpu')
        src_model.load_state_dict(src_checkpoint['model'])
    src_model = src_model.to(device)
    src_model.eval()

    tar_model, tar_mean, tar_std = get_model(args.tar_model)
    if args.ckpt_path:
        from collections import OrderedDict
        ckpt = torch.load(args.ckpt_path)
        state_dict = ckpt['state_dict'] if 'state_dict' in ckpt.keys() else ckpt['model_state_dict']
        new_state_dict = OrderedDict()
        for k,v in state_dict.items():
            if k[0] == '1':
                new_state_dict[k[2:]] = v
            else:
                print('Rejecting', k)
        tar_model.load_state_dict(new_state_dict)

    tar_model = tar_model.to(device)
    tar_model.eval()

    # Setup-Data
    test_loader, test_size = get_data_loader(args)

    # setup eval parameters
    eps = args.eps / 255
    criterion = torch.nn.CrossEntropyLoss()

    clean_acc = 0.0
    adv_acc = 0.0
    fool_rate = 0.0
    logger.info(f'Source: "{args.src_model}" \t Target: "{args.tar_model}" \t Eps: {args.eps} \t Index: {args.index} '
                f'\t Attack: {args.attack_type}')
    with tqdm(enumerate(test_loader), total=len(test_loader)) as p_bar:
        for i, (img, label) in p_bar:
            img, label = img.to(device), label.to(device)

            with torch.no_grad():
                if args.tar_ensemble:
                    clean_out = tar_model(normalize(img.clone(), mean=tar_mean, std=tar_std), get_average=True)
                else:
                    clean_out = tar_model(normalize(img.clone(), mean=tar_mean, std=tar_std))
            if isinstance(clean_out, list):
                clean_out = clean_out[-1].detach()
            clean_acc += torch.sum(clean_out.argmax(dim=-1) == label).item()

            adv = local_adv(src_model, criterion, img, label, eps, attack_type=args.attack_type, iters=args.iter,
                            std=src_std, mean=src_mean, index=args.index, apply_ti=args.apply_ti)

            with torch.no_grad():
                if args.tar_ensemble:
                    adv_out = tar_model(normalize(adv.clone(), mean=tar_mean, std=tar_std), get_average=True)
                else:
                    adv_out = tar_model(normalize(adv.clone(), mean=tar_mean, std=tar_std))
            if isinstance(adv_out, list):
                adv_out = adv_out[-1].detach()
            adv_acc += torch.sum(adv_out.argmax(dim=-1) == label).item()
            fool_rate += torch.sum(adv_out.argmax(dim=-1) != clean_out.argmax(dim=-1)).item()

            if i == 0 and args.save_im:
                vutils.save_image(vutils.make_grid(adv, normalize=False, scale_each=True), 'adv.png')
                vutils.save_image(vutils.make_grid(img, normalize=False, scale_each=True), 'org.png')

            p_bar.set_postfix({"L-inf": f'{(img - adv).max().item() * 255:.2f}',
                               "Clean Range": f'{img.max().item():.2f}, {img.min().item():.2f}',
                               "Adv Range": f'{adv.max().item():.2f}, {adv.min().item():.2f}', })

    print('Clean:{0:.3%}\t Adv :{1:.3%}\t Fooling Rate:{2:.3%}'.format(clean_acc / test_size, adv_acc / test_size,
                                                                       fool_rate / test_size))
    logger.info(
        'Eps:{0} \t Clean:{1:.3%} \t Adv :{2:.3%}\t Fooling Rate:{3:.3%}'.format(int(eps * 255), clean_acc / test_size,
                                                                                 adv_acc / test_size,
                                                                                 fool_rate / test_size))
    json.dump({"eps": int(eps * 255),
               "clean": clean_acc / test_size,
               "adv": adv_acc / test_size,
               "fool rate": fool_rate / test_size, },
              open(f"report/{args.exp}/results.json", "w"), indent=4)


if __name__ == '__main__':
    main()
