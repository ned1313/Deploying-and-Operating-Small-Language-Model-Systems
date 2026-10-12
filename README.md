# Deploying and Operating Small Language Model Systems

These are the exercise files meant to accompany my Pluralsight course of the same name. In the course, you are challenged with the task of using local hardware to run SLMs, attach the SLMs to an agentic workflow, and monitor that workflow for optimization and performance.

## Repository Structure

| Folder | Contents |
| --- | --- |
| [shared/](shared/README.md) | Code, data, and services used by more than one module: the `taco_shared` Python package, the complaint dataset and its generated orders and scenarios, the resolution policy, proposal schemas, and the mock case-management API |
| [m1/](m1/README.md) | Module 1: serve a quantized model with vLLM behind an nginx gateway, screen three candidates, and tune serving under load |
| [m2/](m2/README.md) | Module 2: a LangChain agent with typed tools that investigates a complaint and produces a validated, structured resolution proposal |
| [m3/](m3/README.md) | Module 3: a complaint API running a LangGraph workflow with durable approval checkpoints, bounded retries, clarification and escalation routes, idempotent recording, and failure tests |
| [m4/](m4/README.md) | Module 4: Prometheus, Grafana, and GPU/host exporters across both hosts, correlated structured logs, LangSmith traces, Locust load generation, and a documented performance baseline |

Each module folder has its own compose file, runbooks (`demo-*.md`), and a git-ignored `results/` directory.
[ARCHITECTURE.md](ARCHITECTURE.md) shows how the system evolves from module to module.

## Course Prerequisites

The course assumes that you have access to a system that can run the following:

- Podman with `podman compose`
- NVIDIA Container Toolkit (CDI) for GPU access from containers
- Physical GPU with at least 16GB VRAM

For the demonstrations, I am using a dedicated machine for model hosting. The agentic application is running on a separate system. I chose to do this to keep the model from interfering with my recording software. You can run both the agentic application and model on the same system. The system running the model software is an Ubuntu 26.04 LTS box with an Nvidia GeForce RTX 3090 card.

I chose to run both llama.cpp and vLLM in containers b/c I think it is simpler and cleaner to have isolated inference runtimes. There are just too many dependencies, drivers, and middle-ware that would need to be installed locally. I'd rather not. You can choose to run the model however you'd like. You just need a valid API endpoint for model serving and another to hook into the observability stack.