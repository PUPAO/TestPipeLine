FROM eclipse-temurin:17-jdk AS jdk17
FROM eclipse-temurin:21-jdk AS jdk21

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=jdk17 /opt/java/openjdk /opt/java/jdk-17
COPY --from=jdk21 /opt/java/openjdk /opt/java/jdk-21

ENV JAVA_HOME=/opt/java/jdk-21
ENV PATH="/opt/java/jdk-21/bin:${PATH}"
ENV ORG_GRADLE_PROJECT_org_gradle_java_installations_paths=/opt/java/jdk-17,/opt/java/jdk-21

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ./app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
