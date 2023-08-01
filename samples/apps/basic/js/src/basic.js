let records_table = ccf.kv["records"];

export function put_record(request) {
  const key = request.params.key;
  if (key === undefined) {
    return { statusCode: 404, body: "Missing key" };
  }

  records_table.set(ccf.strToBuf(key), request.body.arrayBuffer());

  return {
    statusCode: 204,
  };
}

export function get_record(request) {
  const key = request.params.key;
  if (key === undefined) {
    return { statusCode: 404, body: "Missing key" };
  }

  const val = records_table.get(ccf.strToBuf(key));
  if (val === undefined) {
    return { statusCode: 404, body: "No such key" };
  }

  return {
    statusCode: 200,
    headers: {
      "content-type": "text/plain",
    },
    body: val,
  };
}
