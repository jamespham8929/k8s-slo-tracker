# The daemon image. `slo-tracker serve` evaluates the SLOs in /config and
# serves the decisions on :9105/metrics.
FROM python:3.13-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY slo_tracker/ slo_tracker/
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.13-slim
RUN useradd --uid 10001 --no-create-home slo
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
USER 10001
EXPOSE 9105
ENTRYPOINT ["slo-tracker", "serve", "--config", "/config/slos.yaml"]
CMD ["--prometheus", "http://prometheus-operated.monitoring:9090"]
