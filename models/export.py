"""Exports a YOLOv3 *.pt model to ONNX and TorchScript formats

Usage:
    $ export PYTHONPATH="$PWD" && python models/export.py --weights ./weights/yolov3.pt --img 640 --batch 1
"""

import argparse
import sys
import time

sys.path.append('./')  # to run '$ python *.py' files in subdirectories

import torch
import torch.nn as nn

from sparseml.pytorch.optim import ScheduledModifierManager
from sparseml.pytorch.utils import ModuleExporter
from sparseml.pytorch.utils.quantization import skip_onnx_input_quantize

import models
from models.experimental import attempt_load
from models.yolo import Model
from utils.activations import Hardswish, SiLU
from utils.general import set_logging, check_img_size
from utils.google_utils import attempt_download
from utils.torch_utils import select_device, intersect_dicts


def load_model(opt, device):
    attempt_download(opt.weights)
    ckpt = torch.load(opt.weights, map_location=device)
    is_picked_model = isinstance(ckpt['model'], nn.Module)

    if not is_picked_model and not opt.cfg:
        raise ValueError(f'{opt.weights} does not load a Module object and no Model cfg given to load into a Module')

    if is_picked_model and not opt.cfg:
        model = attempt_load(opt.weights, map_location=device)  # load FP32 model
        state_dict = model.state_dict()
    else:
        model = Model(opt.cfg or ckpt['model'].yaml)
        state_dict = ckpt['model'].float().state_dict() if is_picked_model else ckpt['model']

    # apply any sparsity or quantization optimizations
    if opt.sparseml_recipe:
        if any([opt.grid, opt.dynamic]):
            raise ValueError("--grid and --dynamic not supported for exports with sparsification")
        manager = ScheduledModifierManager.from_yaml(opt.sparseml_recipe)
        for quant_mod in manager.quantization_modifiers:
            quant_mod.enable_on_initialize = True
        manager.initialize(model, None)

    model.load_state_dict(state_dict, strict=True)  # load
    model.float()
    return model


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--weights', type=str, default='./yolov3.pt', help='weights path')  # from yolov3/models/
    parser.add_argument('--img-size', nargs='+', type=int, default=[640, 640], help='image size')  # height, width
    parser.add_argument('--batch-size', type=int, default=1, help='batch size')
    parser.add_argument('--dynamic', action='store_true', help='dynamic ONNX axes')
    parser.add_argument('--grid', action='store_true', help='export Detect() layer grid')
    parser.add_argument('--device', default='cpu', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--cfg', type=str, default='', help='optional model.yaml path')
    parser.add_argument('--sparseml-recipe', type=str, default=None, help='optional path to sparsification recipe that was used to train this model')
    opt = parser.parse_args()
    opt.img_size *= 2 if len(opt.img_size) == 1 else 1  # expand
    print(opt)
    set_logging()
    t = time.time()

    # Load PyTorch model
    device = select_device(opt.device)
    model = load_model(opt, device)  # load FP32 model
    labels = model.names

    # Checks
    gs = int(max(model.stride))  # grid size (max stride)
    opt.img_size = [check_img_size(x, gs) for x in opt.img_size]  # verify img_size are gs-multiples

    # Input
    img = torch.zeros(opt.batch_size, 3, *opt.img_size).to(device)  # image size(1,3,320,192) iDetection

    # Update model
    for k, m in model.named_modules():
        m._non_persistent_buffers_set = set()  # pytorch 1.6.0 compatibility
        if isinstance(m, models.common.Conv):  # assign export-friendly activations
            if isinstance(m.act, nn.Hardswish):
                m.act = Hardswish()
            elif isinstance(m.act, nn.SiLU):
                m.act = SiLU()
        # elif isinstance(m, models.yolo.Detect):
        #     m.forward = m.forward_export  # assign forward (optional)
    model.model[-1].export = not opt.grid  # set Detect() layer grid export
    y = model(img)  # dry run

    # TorchScript export
    try:
        print('\nStarting TorchScript export with torch %s...' % torch.__version__)
        f = opt.weights.replace('.pt', '.torchscript.pt')  # filename
        ts = torch.jit.trace(model, img, strict=False)
        ts.save(f)
        print('TorchScript export success, saved as %s' % f)
    except Exception as e:
        print('TorchScript export failure: %s' % e)

    # ONNX export
    try:
        import onnx

        print('\nStarting ONNX export with onnx %s...' % onnx.__version__)
        f = opt.weights.replace('.pt', '.onnx')  # filename
        if not opt.sparseml_recipe:
            torch.onnx.export(model, img, f, verbose=False, opset_version=12, input_names=['images'],
                              output_names=['classes', 'boxes'] if y is None else ['output'],
                              dynamic_axes={'images': {0: 'batch', 2: 'height', 3: 'width'},  # size(1,3,640,640)
                                            'output': {0: 'batch', 2: 'y', 3: 'x'}} if opt.dynamic else None)
        else:
            save_dir = "/".join(f.split("/")[:-1])
            save_name = f.split("/")[-1]
            exporter = ModuleExporter(model, save_dir)
            exporter.export_onnx(img, convert_qat=True)
            try:
                skip_onnx_input_quantize(f, f)
            except:
                pass

        # Checks
        onnx_model = onnx.load(f)  # load onnx model
        onnx.checker.check_model(onnx_model)  # check onnx model
        # print(onnx.helper.printable_graph(onnx_model.graph))  # print a human readable model
        print('ONNX export success, saved as %s' % f)
    except Exception as e:
        print('ONNX export failure: %s' % e)

    # CoreML export
    try:
        import coremltools as ct

        print('\nStarting CoreML export with coremltools %s...' % ct.__version__)
        # convert model from torchscript and apply pixel scaling as per detect.py
        model = ct.convert(ts, inputs=[ct.ImageType(name='image', shape=img.shape, scale=1 / 255.0, bias=[0, 0, 0])])
        f = opt.weights.replace('.pt', '.mlmodel')  # filename
        model.save(f)
        print('CoreML export success, saved as %s' % f)
    except Exception as e:
        print('CoreML export failure: %s' % e)

    # Finish
    print('\nExport complete (%.2fs). Visualize with https://github.com/lutzroeder/netron.' % (time.time() - t))
