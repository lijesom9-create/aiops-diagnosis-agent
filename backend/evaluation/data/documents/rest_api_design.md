# RESTful API 设计

## 资源与 URL

RESTful 设计的核心是把系统中的实体抽象为资源，每个资源对应一个 URL。资源名通常使用名词复数形式，例如 `/users`、`/orders`。

## HTTP 方法

不同的 HTTP 方法对应资源的不同操作：

- `GET`：获取资源
- `POST`：创建资源
- `PUT`：更新资源（全量）
- `PATCH`：部分更新资源
- `DELETE`：删除资源

## 状态码

HTTP 状态码用于表示请求的处理结果：

- `200 OK`：请求成功
- `201 Created`：资源创建成功
- `400 Bad Request`：请求参数错误
- `401 Unauthorized`：未认证
- `403 Forbidden`：无权限
- `404 Not Found`：资源不存在
- `500 Internal Server Error`：服务器内部错误

合理设计 URL、方法和状态码，可以让 API 更加直观和易用。
