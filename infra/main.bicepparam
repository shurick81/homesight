using 'main.bicep'

// Object id of the user that runs tools/cloud/provision.py (az ad signed-in-user show --query id -o tsv)
param loaderPrincipalId = '878ebc1a-141d-468d-aaa8-1eea433c80bf'

// The live read-model database. Changed only by tools/cloud/provision.py flip.
param activeDatabase = 'homesight_blue'
