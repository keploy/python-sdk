
def get_request_data(request) -> dict:
    req_data = {}

    req_data['header'] = {key: [value] for key,value in request.headers.to_wsgi_list()}
    req_data['method'] = request.method
    req_data['body'] = request.get_data(as_text=True)
    # req_data['form_data'] = request.form.to_dict()
    # req_data['file_data'] =  { k: v[0].read() for k, v in request.files.lists()}
    req_data['uri'] = request.url_rule.rule
    req_data['url'] = request.path
    req_data['base'] = request.url
    req_data['params'] = request.args.to_dict()

    protocol = request.environ.get('SERVER_PROTOCOL', None)
    if protocol:
        req_data['proto_major'] = int(protocol.split(".")[0][-1])
        req_data['proto_minor'] = int(protocol.split(".")[1])

    return req_data