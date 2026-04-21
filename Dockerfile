FROM public.ecr.aws/lambda/python:3.11

COPY ingestion/ ./ingestion/
COPY lambda/ ./lambda/
COPY requirements.txt .

RUN pip install --upgrade pip
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

CMD ["lambda.handler.lambda_handler"]