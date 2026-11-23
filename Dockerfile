FROM eclipse-temurin:21-jdk AS jdk21

FROM python:3.11-slim

WORKDIR /app

# JDK 21 복사 및 환경변수 등록 (타겟 Spring Boot의 ./gradlew test 실행용)
COPY --from=jdk21 /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="${JAVA_HOME}/bin:${PATH}"

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ./app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
