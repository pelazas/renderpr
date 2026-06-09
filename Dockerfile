FROM 303859149452.dkr.ecr.eu-west-1.amazonaws.com/renderpr-base:latest

WORKDIR /app

COPY src/ /app/src/

CMD ["python", "-u", "-m", "src.agent.main"]
