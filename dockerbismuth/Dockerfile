FROM python:3.6-slim

RUN mkdir /code
WORKDIR /code

COPY requirements.txt /code
RUN apt-get update && apt-get install gcc autoconf -y
RUN pip install -r requirements.txt

COPY . /code

COPY peers_node.txt /code/peers.txt
COPY peers_node.txt /code/suggested_peers.txt
