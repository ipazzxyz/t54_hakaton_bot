FROM python:latest
USER root
CMD ["python", "main.py"]

RUN apt-get update && apt-get install -y && pip install --upgrade pip 
RUN mkdir -p /app
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt