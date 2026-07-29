# 消息队列对比：RabbitMQ vs Kafka

## 消息队列概述

消息队列是分布式系统中用于解耦服务、异步处理和削峰填谷的核心中间件。主流方案有 RabbitMQ 和 Apache Kafka。

## 核心指标对比

| 指标 | RabbitMQ | Kafka | Pulsar |
|------|----------|-------|--------|
| 吞吐量 | 万级/秒 | 百万级/秒 | 百万级/秒 |
| 延迟 | 微秒级 | 毫秒级 | 毫秒级 |
| 持久化 | 支持 | 支持 | 支持 |
| 消息回溯 | 不支持 | 支持 | 支持 |
| 消息顺序 | 队列内有序 | 分区内有序 | 分区内有序 |
| 协议 | AMQP/MQTT/STOMP | 自定义协议 | 自定义协议 |
| 适用场景 | 低延迟业务消息 | 日志/流处理 | 混合场景 |

## RabbitMQ 工作模式

### 工作队列模式

```python
import pika

connection = pika.BlockingConnection(pika.ConnectionParameters('localhost'))
channel = connection.channel()

channel.queue_declare(queue='task_queue', durable=True)

# 发送消息
channel.basic_publish(
    exchange='',
    routing_key='task_queue',
    body='Hello World',
    properties=pika.BasicProperties(delivery_mode=2)  # 持久化
)

# 消费消息
def callback(ch, method, properties, body):
    print(f"Received: {body}")
    ch.basic_ack(delivery_tag=method.delivery_tag)

channel.basic_consume(queue='task_queue', on_message_callback=callback)
channel.start_consuming()
```

## Kafka 生产者示例

```python
from kafka import KafkaProducer
import json

producer = KafkaProducer(
    bootstrap_servers=['localhost:9092'],
    value_serializer=lambda v: json.dumps(v).encode('utf-8'),
    acks='all',  # 等待所有副本确认
    retries=3,
)

for i in range(100):
    producer.send('test_topic', {'number': i})

producer.flush()
```

## 选型建议

| 场景 | 推荐 | 理由 |
|------|------|------|
| 订单系统 | RabbitMQ | 低延迟，需要消息确认 |
| 日志收集 | Kafka | 高吞吐，允许少量丢失 |
| 实时流处理 | Kafka | 与 Spark/Flink 集成好 |
| IoT 设备消息 | RabbitMQ | 支持 MQTT 协议 |
| 事件溯源 | Kafka | 支持消息回溯 |
