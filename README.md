# Omniverse 3D Gaussian Splatting Extension

3D Gaussian Splatting (3DGS) extension for Omniverse.

> For Neural Radiance Field (NeRF) support, please refer to the [Omniverse NeRF Extension](https://github.com/j3soon/omni-nerf-extension).

## Prerequisites

- **Hardware**:
  - CPU: x86
  - GPU: NVIDIA RTX GPU
  - See [this page](https://docs.omniverse.nvidia.com/isaacsim/latest/installation/requirements.html#system-requirements) for more details.
- **Operating System**: Ubuntu 20.04/22.04.
- **Software**:
  - [NVIDIA Driver](https://ubuntu.com/server/docs/nvidia-drivers-installation)
  - [Docker](https://docs.docker.com/engine/install/ubuntu/)
  - [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
  - Omniverse Isaac Sim (through NGC Container)

## Demo

> TODO: Add more demo videos here.

**Note**: The datasets for these demos are not provided in this repository as they are casually collected. The purpose of these demos is to illustrate that this repository can be readily applied to a variety of custom datasets with extremely low effort. The following guide will use the `Poster` dataset for simplicity.

## Setup

```sh
git clone https://github.com/j3soon/omni-3dgs-extension
cd omni-3dgs-extension
```

Download assets:

```sh
wget https://github.com/j3soon/omni-3dgs-extension/releases/download/v0.0.1/assets.zip
unzip assets.zip
```

Prepare assets for `vanillags_renderer`:

Rename the timestamp and checkpoint files to the same name as the placeholder for simplicity:

```sh
# change the DATE_TIME to the name of the placeholder
DATE_TIME=2025-02-19_105311
cp -r ./assets/exports/poster/splatfacto/$DATE_TIME ./assets/exports/poster/splatfacto/DATE_TIME
DATE_TIME=2025-02-19_121606
cp -r ./assets/exports/poster/nerfacto/$DATE_TIME ./assets/exports/poster/nerfacto/DATE_TIME
```

You can check if the renaming succeeded with the following command:

```sh
ls ./assets/exports/poster/splatfacto/DATE_TIME/splat/splat.ply
ls ./assets/exports/poster/nerfacto/DATE_TIME/tsdf/mesh.obj
```

The following also assumes that you are running the commands from the root of the repository.

## Managing Containers

Build the docker images for the extension:

```sh
docker compose build
```

Launch the containers:

```sh
# You might want to use `tmux` for exec-ing into the containers later
xhost +local:docker
docker compose up
```

Then follow the remaining sections.

To remove and stop the containers, run:

```sh
docker compose down
```

### VanillaGS Renderer

Code: [`vanillags_renderer`](./vanillags_renderer)

Attach to the container and start the renderer:

```sh
docker exec -it vanillags-renderer bash -ic "python /src/main.py"
```

### PyGame Viewer

Code: [`pygame_viewer`](./pygame_viewer)

Attach to the container and run the testing script:

```sh
docker exec -it pygame-viewer /src/run.sh
```

### Isaac Sim Viewer

Code: [`extension`](./extension)

```sh
docker exec -it isaac-sim-viewer bash
# in container
/isaac-sim/runapp.sh --ext-folder /src/exts --enable omni.gsplat.viewport
```

> Alternatively, you can use WebRTC by running:
> 
> ```sh
> # in container
> /isaac-sim/runheadless.webrtc.sh --ext-folder /src/exts --enable omni.gsplat.viewport
> ```
> 
> Wait for the `Isaac Sim Headless WebRTC App is loaded.` message,
> and then visit <http://127.0.0.1:8211/streaming/webrtc-demo/?server=127.0.0.1> in Google Chrome.

![](docs/media/isaac-sim-steps.png)

1. Select the folder `/workspace/usd`
2. Open the file `example_scene.usd`
3. Click the mesh that you added in Step 2.
4. Press the button in 3DGS Viewport to update the input mesh of 3DGS.

**Known Issues**:
- Cannot correctly handling non-uniform scaling of the object mesh yet.

## Development Notes

### VanillaGS Renderer

After modifying code, you need to re-run the main renderer script. The docker container can be re-used since the code is mounted as a volume.

### PyGame Viewer

After modifying code, you need to re-run the testing script. The docker container can be re-used since the code is mounted as a volume.

### Isaac Sim Viewer

Setup VSCode intellisense for the extension:

```sh
cd extension
./link_app.sh --path "$HOME/.local/share/ov/pkg/code-2022.3.3"
# open the `extension` folder in VSCode
```

After modifying code, you can restart Isaac Sim to apply changes. The docker container can be re-used since the code is mounted as a volume. If the change is small, it is often faster to disable and re-enable the extension in the Isaac Sim UI. This can be done through `Window > Extensions > NVIDIA > General`, search `nerf`, and then un-toggle and re-toggle the extension.

## Future Directions

- Support multiple 3DGS/USD renderings in a single scene potentially through [Compositioning](https://docs.nerf.studio/extensions/blender_addon.html#compositing-nerf-objects-in-nerf-environments).
- Communication between the renderer and the viewer is currently done through ZMQ IPC and JEPG compression. However it may be more efficient to use [CUDA IPC](https://github.com/NVIDIA/cuda-samples/tree/master/Samples/0_Introduction/simpleIPC) to bypass copy between GPU memory and CPU memory.
- Include more 3DGS renderers.

## Related Works

This project focuses on the simplest integration of various 3DGS renderers with Omniverse by intentionally decoupling the renderer backend from the Omniverse extension. This design enables easy future integration of advanced 3DGS renderers that use representations incompatible with Vanilla Gaussian Splatting, such as [Compact 3DGS](https://maincold2.github.io/c3dgs/), [Octree-GS](https://city-super.github.io/octree-gs/), and others. This allows for rapid prototyping without the need of standardizing the representation of 3DGS.

It is worth noting that advanced usages, such as those (shadows, reflections, refractions) proposed in [a talk from GTC Spring 2023](https://www.nvidia.com/en-us/on-demand/session/gtcspring23-s52163/), are out of scope of this project. The formal way to integrate 3DGS with Omniverse may need to somehow standardize the representation of 3DGS, and refer to methods such as [3DGUT](https://research.nvidia.com/labs/toronto-ai/3DGUT/) and [3DGRT](https://gaussiantracer.github.io/).

This project originated as a feature branch of the [j3soon/omni-nerf-extension](https://github.com/j3soon/omni-nerf-extension). However, the branch has diverged significantly and we decided to maintain this project separately. Key differences include:

- Switched to ZMQ instead of RPyC for communication between the renderer and viewer, enabling full decoupling of codebases since RPyC requires matching Python versions.
- Uses Inter-Process Communication (IPC) instead of network communication for faster data transfer between the renderer and viewer.
- Removed progressive rendering since 3DGS rendering is much faster than NeRF and doesn't require it.
- Directly invoke the 3DGS renderer instead of using the Nerfstudio pipeline to enable easier integration of different 3DGS renderers, avoiding tight coupling with the Nerfstudio codebase which is difficult to trace and maintain.

There are also other projects that attempt to integrate 3DGS with Omniverse, such as [tangkangqi/omni-gaussian-splatting-extension](https://github.com/tangkangqi/omni-gaussiansplating-extension). However, I haven't tried them yet.

## Acknowledgement

This project has been made possible through the support of [CGV Lab][cgvlab], [VMLab](vmlab), and [NVIDIA AI Technology Center (NVAITC)][nvaitc].

Special thanks to [@tigerpaws01](https://github.com/tigerpaws01) for the initial implementation of the PyGame viewer.

I would also like to thank the large-scale 3DGS Study Group members, [@Sunnyhong0326](https://github.com/Sunnyhong0326) and Ting-Yu Yan for discussions.

For a complete list of contributors to the code of this repository, please visit the [contributor list](https://github.com/j3soon/omni-3dgs-extension/graphs/contributors).

[cgvlab]: https://cgv.cs.nthu.edu.tw
[vmlab]: https://vmlab-nthu.notion.site/NTHU-VMLab-143b8d611ddc8071ab0ede97aacfc403?pvs=4
[nvaitc]: https://github.com/NVAITC

Disclaimer: this is not an official NVIDIA product.
