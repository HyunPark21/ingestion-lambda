FROM public.ecr.aws/lambda/python:3.11

COPY ingestion/ ./ingestion/
COPY lambda/ ./lambda/
COPY requirements.txt .

RUN pip install -r requirements.txt

CMD ["lambda.handler.lambda_handler"]