# Train Your Own 3DGS Model

The following instructions assume you are in the git repository root:

```sh
git clone https://github.com/j3soon/omni-gsplat-extension.git
cd omni-gsplat-extension
```

## Preparing the Dataset and COLMAP Results

For consistent results, ensure you use the same dataset and COLMAP data for both training and mesh extraction. While you can use different methods for training and mesh extraction (e.g., training with 3DGS and extracting with NeRF), the underlying dataset and COLMAP reconstruction must remain identical.

### Prepare with NeRF Studio

Follow the [installation](https://docs.nerf.studio/quickstart/installation.html#use-docker-image) guide, specifically:

```sh
mkdir assets
docker run --rm -it --gpus all \
  -v $(pwd)/assets:/workspace/ \
  -v $HOME/.cache/:/home/user/.cache/ \
  -p 7007:7007 \
  -w /workspace \
  --shm-size=12gb \
  ghcr.io/nerfstudio-project/nerfstudio:1.1.5
# in the nerfstudio container
# Download some test data:
ns-download-data nerfstudio --capture-name=poster
```

### Prepare with Vanilla Gaussian Splatting

> **Note**: The instructions on using Vanilla Gaussian Splatting is still under construction.
> Since we're still surveying suitable methods for mesh extraction, please use NeRF Studio for now.

Launch the COLMAP container and process the images:

```sh
mkdir -p assets/gaussian_splatting
cp -r ./assets/data/nerfstudio/poster/images ./assets/gaussian_splatting/input
wget https://raw.githubusercontent.com/j3soon/gaussian-splatting/refs/heads/main/convert.py -O ./assets/gaussian_splatting/convert.py
docker run --rm -it --gpus all -w /working -v $(pwd)/assets/gaussian_splatting:/working colmap/colmap
# in the colmap container
# Patch the convert.py for ImageMagick 6
sed -i 's|os.system(magick_command + " mogrify|os.system("mogrify|g' ./convert.py
apt-get update && apt-get install -y python3 imagemagick
python3 convert.py -s /working --resize
```

## Preparing a 3DGS Model

Follow one of the following sections to prepare a 3DGS model.

### Train with Splatfacto

Follow the [training model](https://docs.nerf.studio/quickstart/first_nerf.html) guide and the [Splatfacto](https://docs.nerf.studio/nerfology/methods/splat.html) guide, specifically:

```sh
# in the nerfstudio container
# Train default model
ns-train splatfacto --data data/nerfstudio/poster
# or train big model with more gaussians (preferred)
ns-train splatfacto-big --data data/nerfstudio/poster
# wait for training to finish
```

You can view the trained 3DGS model using the Nerfstudio viewer by following the [viewer guide](https://docs.nerf.studio/quickstart/viewer_quickstart.html):

```sh
# in the nerfstudio container
# change the DATE_TIME to the actual value
DATE_TIME=2025-02-19_105311
# View the model
ns-viewer --load-config outputs/poster/splatfacto/$DATE_TIME/config.yml
# open the printed URL
```

Export the gaussian splats to a ply file:

```sh
# in the nerfstudio container
# change the DATE_TIME to the actual value
DATE_TIME=2025-02-19_105311
# Export gaussian splat
ns-export gaussian-splat --load-config outputs/poster/splatfacto/$DATE_TIME/config.yml --output-dir exports/poster/splatfacto/$DATE_TIME/splat/
```

View the ply file (`splat.ply`) in [Three.js Gaussian Splatting Viewer](https://projects.markkellogg.org/threejs/demo_gaussian_splats_3d.php) or any other 3DGS viewer.

### Train with Vanilla Gaussian Splatting

Alternatively, you can train a 3DGS model with the [Vanilla Gaussian Splatting](https://github.com/graphdeco-inria/gaussian-splatting) codebase.

Launch the 3DGS container and train the 3DGS model:

```sh
xhost +local:docker
docker run --rm -it --gpus all --name gaussian_splatting \
  --shm-size=64g \
  --device /dev/dri:/dev/dri \
  -e DISPLAY=$DISPLAY \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v $HOME/.Xauthority:/root/.Xauthority \
  -v $(pwd)/assets/gaussian_splatting:/workspace/data \
  j3soon/gaussian_splatting bash
# in the gaussian_splatting container
python train.py -s /workspace/data -m /workspace/data/output/poster
```

Finally, launch the 3DGS container with GUI support and visualize the 3DGS model:

```sh
# in the gaussian_splatting container
./SIBR_viewers/install/bin/SIBR_gaussianViewer_app -m /workspace/data/output/poster
```

If you encounter the following error:

```
Error Using Interop

This application tries to use CUDA/OpenGL interop.
 It did NOT work for your current configuration.
 For highest performance, OpenGL and CUDA must run on the same
 GPU on an OS that supports interop. You can try to pass a
 non-zero index via --device on a multi-GPU system, and/or try
 attaching the monitors to the main CUDA card.
 On a laptop with one integrated and one dedicated GPU, you can try
 to set the preferred GPU via your operating system.

 FALLING BACK TO SLOWER RENDERING WITH CPU ROUNDTRIP
```

Run the following command instead:

```sh
# in the gaussian_splatting container
__NV_PRIME_RENDER_OFFLOAD=1 __GLX_VENDOR_LIBRARY_NAME=nvidia ./SIBR_viewers/install/bin/SIBR_gaussianViewer_app -m /workspace/data/output/poster
```

Alternatively, view the ply file (`point_cloud.ply`) in [Three.js Gaussian Splatting Viewer](https://projects.markkellogg.org/threejs/demo_gaussian_splats_3d.php) or any other 3DGS viewer.

## Preparing a Mesh

Follow one of the following sections to prepare a mesh.

### Train with Nerfacto

Since Splatfacto doesn't support mesh export, we need to train a model that does. The simplest model that supports mesh export is `nerfacto`.

Follow the [training model](https://docs.nerf.studio/quickstart/first_nerf.html) guide, specifically:

```sh
# in the nerfstudio container
# Train model with normal prediction
ns-train nerfacto --data data/nerfstudio/poster --pipeline.model.predict-normals True
# wait for training to finish
```

You can view the trained mesh using the Nerfstudio viewer by following the [viewer guide](https://docs.nerf.studio/quickstart/viewer_quickstart.html):

```sh
# in the nerfstudio container
# change the DATE_TIME to the actual value
DATE_TIME=2025-02-19_121606
# View the model
ns-viewer --load-config outputs/poster/nerfacto/$DATE_TIME/config.yml
# open the printed URL
```

Export the mesh using the [export geometry](https://docs.nerf.studio/quickstart/export_geometry.html) guide, specifically:

```sh
# in the nerfstudio container
# change the DATE_TIME to the actual value
DATE_TIME=2025-02-19_121606
# Export mesh
ns-export tsdf --load-config outputs/poster/nerfacto/$DATE_TIME/config.yml --output-dir exports/poster/nerfacto/$DATE_TIME/tsdf/ --target-num-faces 50000 --num-pixels-per-side 2048 --use-bounding-box True --bounding-box-min -0.55 -0.25 -0.55 --bounding-box-max 0.15 0.45 0.15
```

Or use [Poisson Surface Reconstruction](https://docs.nerf.studio/quickstart/export_geometry.html#poisson-surface-reconstruction) instead, if the network supports predicting normals. Note that you will need to adjust the export parameters to achieve optimal mesh quality.

View the mesh (`mesh.obj`) in Blender or any other 3D viewer.
