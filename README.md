# Deploying and Operating Small Language Model Systems

These are the exercise files meant to accompany my Pluralsight course of the same name. In the course, you are challenged with the task of using local hardware to run SLMs, attach the SLMs to an agentic workflow, and monitor that workflow for optimization and performance.

## Repository Structure

## Course Prerequisites

The course assumes that you have access to a system that can run the following:

- Podman with `podman compose`
- NVIDIA Container Toolkit (CDI) for GPU access from containers
- Physical GPU with at least 16GB VRAM

For the demonstrations, I am using a dedicated machine for model hosting. The agentic application is running on a separate system. I chose to do this to keep the model from interfering with my recording software. You can run both the agentic application and model on the same system. The system running the model software is an Ubuntu 26.04 LTS box with an Nvidia GeForce RTX 3090 card.

I chose to run both llama.cpp and vLLM in containers b/c I think it is simpler and cleaner to have isolated inference runtimes. There are just too many dependencies, drivers, and middle-ware that would need to be installed locally. I'd rather not. You can choose to run the model however you'd like. You just need a valid API endpoint for model serving and another to hook into the observability stack.