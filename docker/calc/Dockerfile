# Step 24.4b (design-steward 7.8.24 "十" 2, 6): the steward's calculate sandbox. The DSH host runs
# it as a disposable container per call (integrations/dsh-driver/calc.mjs: no network, read-only
# root, uid 65534, no capability, two small tmpfs, the job's input directory read-only) and knows
# it only by its image id: build it, then put `docker image inspect --format '{{.Id}}' <tag>` into
# the host's environment as AITELIER_CALC_IMAGE (sha256:<64 hex>; anything else: calculate is off).
#
#   docker build -t aitelier-calc:<date> docker/calc
#
# The base is pinned by digest (python:3.12-slim, Python 3.12.16, linux/amd64) and every library
# by version and wheel hash (requirements.txt). No network tool is installed; the container never
# has a network anyway.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

COPY requirements.txt /opt/calc/requirements.txt
# matplotlib's font cache is made once here; run.py copies it into the writable /tmp.
RUN pip install --no-cache-dir --disable-pip-version-check --require-hashes --only-binary=:all: \
        -r /opt/calc/requirements.txt \
    && MPLCONFIGDIR=/opt/calc/mpl MPLBACKEND=Agg python -c "import matplotlib.pyplot" \
    && mkdir -p /job/in /job/out \
    && chmod -R a+rX /opt/calc

COPY run.py /opt/calc/run.py
RUN chmod 0644 /opt/calc/run.py

ENV MPLBACKEND=Agg MPLCONFIGDIR=/tmp/matplotlib HOME=/tmp
USER 65534:65534
WORKDIR /job/out
ENTRYPOINT ["python3", "-I", "-B", "/opt/calc/run.py"]
