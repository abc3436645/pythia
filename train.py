import timeit
import argparse
import random
import os
import demjson
import yaml
from torch import optim
from torch.utils.data import DataLoader
from config.config_utils import finalize_config, dump_config
from config.config import cfg
from global_variables.global_variables import use_cuda
from train_model.dataset_utils import prepare_train_data_set, prepare_eval_data_set, prepare_test_data_set
from train_model.helper import build_model, run_model, print_result
from train_model.Loss import get_loss_criterion
from train_model.Engineer import one_stage_train
import glob
import torch
from torch.optim.lr_scheduler import LambdaLR
from bisect import bisect
import gc
import operator as op
import functools

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, required=False, help="config yaml file")
    parser.add_argument("--out_dir", type=str, default=None, help="output directory, default is current directory")
    parser.add_argument('--seed', type=int, default=1234,
                        help='random seed, default 1234, set seed to -1 if need a random seed between 1 and 100000')
    parser.add_argument('--config_overwrite', type=str, help="a json string to update yaml config file", default=None)
    parser.add_argument("--force_restart", action='store_true',
                        help="flag to force clean previous result and restart training")

    arguments = parser.parse_args()
    return arguments


def process_config(config_file, config_string):
    finalize_config(cfg, config_file, config_string)


def get_output_folder_name(config_basename, cfg_overwrite_obj, seed):
    m_name, _ = os.path.splitext(config_basename)

    if cfg_overwrite_obj is not None:
        f_name = yaml.safe_dump(cfg_overwrite_obj, default_flow_style=False)
        f_name = f_name.replace(':', '.').replace('\n', ' ').replace('/', '_')
        f_name = ' '.join(f_name.split())
        f_name = f_name.replace('. ', '.').replace(' ', '_')
        f_name += '_%d' % seed
    else:
        f_name = '%d' % seed

    return m_name, f_name


def lr_lambda_fun(i_iter):
    if i_iter <= cfg.training_parameters.wu_iters:
        alpha = float(i_iter) / float(cfg.training_parameters.wu_iters)
        factor =  cfg.training_parameters.wu_factor * (1. - alpha) + alpha
        return factor
    else:
        idx = bisect(cfg.training_parameters.lr_steps, i_iter)
        factor = pow(cfg.training_parameters.lr_ratio, idx)
        return factor

def get_optim_scheduler(optimizer):
    return LambdaLR(optimizer, lr_lambda=lr_lambda_fun)


def print_eval(prepare_data_fun, out_label):
    model_file = os.path.join(snapshot_dir, "best_model.pth")
    pkl_res_file = os.path.join(snapshot_dir, "best_model_predict_%s.pkl" % out_label)
    out_file = os.path.join(snapshot_dir, "best_model_predict_%s.json" % out_label)

    data_set_test = prepare_data_fun(**cfg['data'], **cfg['model'], verbose=True)
    data_reader_test = DataLoader(data_set_test, shuffle=False,
                                  batch_size=cfg.data.batch_size, num_workers=cfg.data.num_workers)
    ans_dic = data_set_test.answer_dict

    model = build_model(cfg, data_set_test)
    model.load_state_dict(torch.load(model_file)['state_dict'])

    question_ids, soft_max_result, _, _ = run_model(model, data_reader_test, ans_dic.UNK_idx)
    print_result(question_ids, soft_max_result, ans_dic, out_file, json_only=False, pkl_res_file=pkl_res_file, test=out_label)


if __name__ == '__main__':
    start = timeit.default_timer()

    args = parse_args()
    config_file = args.config
    seed = args.seed if args.seed > 0 else random.randint(1, 100000)
    process_config(config_file, args.config_overwrite)

    torch.manual_seed(seed)
    if use_cuda:
        torch.cuda.manual_seed(seed)


    basename = 'default' if args.config is None else os.path.basename(args.config)

    cmd_cfg_obj = demjson.decode(args.config_overwrite) if args.config_overwrite is not None else None

    middle_name, final_name = get_output_folder_name(basename, cmd_cfg_obj, seed)

    out_dir = args.out_dir if args.out_dir is not None else os.getcwd()

    snapshot_dir = os.path.join(out_dir, "results", middle_name, final_name)
    boards_dir = os.path.join(out_dir, "boards", middle_name, final_name)

    if not os.path.exists(snapshot_dir):
        os.makedirs(snapshot_dir)
    if not os.path.exists(boards_dir):
        os.makedirs(boards_dir)

    print("snapshot_dir=" + snapshot_dir)
    print("fast data reader = " + str(cfg['data']['image_fast_reader']))
    print("use cuda = " + str(use_cuda))

    # dump the config file to snap_shot_dir
    config_to_write = os.path.join(snapshot_dir, "config.yaml")
    dump_config(cfg, config_to_write)

    train_dataSet = prepare_train_data_set(**cfg['data'], **cfg['model'])

    my_model = build_model(cfg, train_dataSet)

    model = my_model
    if hasattr(my_model, 'module'):
        model = my_model.module

    params = [{'params': model.image_embedding_models_list.parameters()},
              {'params': model.question_embedding_models.parameters()},
              {'params': model.multi_modal_combine.parameters()},
              {'params': model.classifier.parameters()},
              {'params': model.image_feature_encode_list.parameters(),
              'lr': cfg.optimizer.par.lr * 0.1}]
    # if model.image_text_feature_encode_list is not None:
    #     params += [{'params': model.image_text_feature_encode_list.parameters()}]
    if model.image_text_feat_embedding_models_list is not None:
        params += [{'params': model.image_text_feat_embedding_models_list.parameters(),
                     'lr': cfg.model.itf_lr}]

    my_optim = getattr(optim, cfg.optimizer.method)(params, **cfg.optimizer.par)
    print(cfg.training_parameters)
    print("Learning rate  " + str(cfg.optimizer.par.lr))

    i_epoch = 0
    i_iter = 0
    if not args.force_restart:
        md_pths = os.path.join(snapshot_dir, "model_*.pth")
        files = glob.glob(md_pths)
        if len(files) > 0:
            latest_file = max(files, key=os.path.getctime)
            info = torch.load(latest_file)
            i_epoch = info['epoch']
            i_iter = info['iter']
            sd = info['state_dict']
            op_sd = info['optimizer']
            my_model.load_state_dict(sd)
            my_optim.load_state_dict(op_sd)

    scheduler = get_optim_scheduler(my_optim)

    my_loss = get_loss_criterion(cfg.loss)
    if cfg.att_loss:
        my_att_loss = get_loss_criterion(cfg.att_loss)
    else:
        my_att_loss = my_loss

    if cfg.ans_loss:
        my_ans_loss = get_loss_criterion(cfg.ans_loss)
    else:
        my_ans_loss = my_loss

    data_set_val = prepare_eval_data_set(**cfg['data'], **cfg['model'])

    data_reader_trn = DataLoader(dataset=train_dataSet, batch_size=cfg.data.batch_size, shuffle=True,
                                 num_workers=cfg.data.num_workers)
    data_reader_val = DataLoader(data_set_val, shuffle=True, batch_size=cfg.data.batch_size,
                                 num_workers=cfg.data.num_workers)

    print("BEGIN TRAINING MODEL...")

    total = 0
    print("Before training")
    for obj in gc.get_objects():
        try:
            if torch.is_tensor(obj) or (hasattr(obj, 'data') and torch.is_tensor(obj.data)):
                print(functools.reduce(op.mul, obj.size()) if len(obj.size()) > 0 else 0, type(obj), obj.size())
                total += functools.reduce(op.mul, obj.size())
        except:
            continue

    print(str(total*4.0/(10**9)) + " GB")

    use_attention_supervision = cfg.model.use_attention_supervision
    use_answer_supervision = cfg.model.use_answer_supervision
    one_stage_train(my_model, data_reader_trn, my_optim, my_loss, data_reader_eval=data_reader_val,
                    snapshot_dir=snapshot_dir, log_dir=boards_dir, start_epoch=i_epoch, i_iter=i_iter,
                    scheduler=scheduler, use_attention_supervision=use_attention_supervision,
                    use_answer_supervision=use_answer_supervision,
                    att_loss_criterion=my_att_loss,
                    ans_loss_criterion=my_ans_loss,
                    att_loss_weight=cfg.model.att_loss_weight,
                    ans_loss_weight=cfg.model.ans_loss_weight)

    print("After training")
    total = 0
    for obj in gc.get_objects():
        try:
            if torch.is_tensor(obj) or (hasattr(obj, 'data') and torch.is_tensor(obj.data)):
                print(functools.reduce(op.mul, obj.size()) if len(obj.size()) > 0 else 0, type(obj), obj.size())
                total += functools.reduce(op.mul, obj.size())
        except:
            continue

    print(str(total*4.0/(10**9)) + " GB")


    print("BEGIN PREDICTING ON TEST/VAL set...")

    if 'predict' in cfg.run:
        print("Running on test")
        print_eval(prepare_test_data_set, "test")
    if cfg.run == 'train+val':
        print_eval(prepare_eval_data_set, "val")

    end = timeit.default_timer()
    total_time = (end - start) / 3600.0

    print("total runtime(h): %.2f" % total_time)
