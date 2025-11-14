## Для работы необходимо записать в dotenv:

1. Ключ OpenRouter
2. Токен бота Max

Example **.env**

```
OPENROUTER_API_KEY="ключ"
MAXBOT_TOKEN="токен"
```

## Сборка и запуск.

1. Собрать контейнер
   `docker build -t t54_hakaton_bot .`
2. Запустить контейнер
   `docker run t54_hakaton_bot`

В одну строку: `docker build -t t54_hakaton_bot .&& docker run t54_hakaton_bot`
