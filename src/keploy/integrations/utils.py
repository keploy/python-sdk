import yaml

def write_dedup(result, id):
    filePath = 'dedupData.yaml'
    existingData = []
    try:
        with open(filePath, 'r') as file:
            existingData=yaml.safe_load(file)
    except:
        with open(filePath, 'w') as file:
            pass

    yaml_data = {
        'id': id,
        'executedLinesByFile': {}
    }
    for file in result.measured_files():
        yaml_data['executedLinesByFile'][file] = result.lines(file)
    if existingData is None:
        existingData=[]
    existingData.append(yaml_data)
    with open(filePath, 'w') as file:
        yaml.dump(existingData, file, sort_keys=False)