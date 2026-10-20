FROM python:3.12.12-alpine3.21

ARG INFRA_GIT_REVISION=unknown
ENV INFRA_WEB_BUILD_REVISION=${INFRA_GIT_REVISION}

WORKDIR /opt/infra-web
COPY --chmod=0444 app.py admin.py /opt/infra-web/

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

CMD ["python3", "/opt/infra-web/admin.py"]
