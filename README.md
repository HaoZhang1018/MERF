# MERF
This is the official code for the paper "A Practical HDR-like Image Generator via Mutual-guided Learning between Multi-exposure Registration and Fusion"

````
@article{hong2024merf,
  title={MERF: A Practical HDR-Like Image Generator via Mutual-Guided Learning Between Multi-Exposure Registration and Fusion},
  author={Hong, Wenhui and Zhang, Hao and Ma, Jiayi},
  journal={IEEE Transactions on Image Processing},
  volume={33},
  pages={2361--2376},
  year={2024},
  publisher={IEEE}
}
````

## Environment Preparing

```
python 3.6
pytorch 1.7.0
visdom 0.1.8.9
dominate 2.6.0
timm 0.6.12
Pillow 9.4.0
```

### Testing

We provide some example images for testing in `./test_data/`

```
python test.py --dataroot your/data/root
```
