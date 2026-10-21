FROM python:3.11-slim

WORKDIR /app

# Устанавливаем системные библиотеки для работы Pillow (генерация капчи)
RUN apt-get update && apt-get install -y \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Запускаем твоего бота
CMD ["python", "main.py"]
