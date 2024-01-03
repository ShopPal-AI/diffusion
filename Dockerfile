FROM ghcr.io/shoppal-ai/llm-base:latest

LABEL maintainer="Guangbin Zhu <zhuguangbin@shoppal.ai>"

COPY assets/ /workspace/assets/
COPY diffusion/ /workspace/diffusion/
COPY scripts/ /workspace/scripts/
COPY yamls/ /workspace/yamls/
COPY tests/ /workspace/tests/
COPY run.py /workspace/run.py
COPY run_eval.py /workspace/run_eval.py
COPY setup.py /workspace/setup.py
COPY pyproject.toml /workspace/pyproject.toml
COPY Makefile /workspace/Makefile

WORKDIR /workspace
RUN pip install -e .
RUN pip install torch==2.0.1+cu118 -f https://download.pytorch.org/whl/cu118/torch_stable.html
RUN pip install torchvision==0.15.2+cu118 -f https://download.pytorch.org/whl/cu118/torch_stable.html
