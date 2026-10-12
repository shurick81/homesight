// homesight cloud: a read-only API over the BRF data, billed per operation only.
//   Cosmos DB for NoSQL, serverless  - pay per request unit + GB stored, nothing while idle
//   Azure Functions, Flex Consumption - pay per execution + GB-s, scales to zero (no always-ready instances)
// Identity-based access throughout: the Function reads Cosmos and its storage with its managed identity,
// Cosmos local (key) auth is disabled, and the deploying user gets data access for the loader.
//
// Databases and containers are not declared here: tools/cloud/provision.py creates them blue/green
// (homesight_blue / homesight_green) and `activeDatabase` says which one the Function reads.
//
//   az deployment group create -g homesight -f infra/main.bicep -p infra/main.bicepparam

param location string = resourceGroup().location
param prefix string = 'homesight'
@description('Object id of the user (or service principal) that runs tools/cloud/provision.py.')
param loaderPrincipalId string
@description('Upper bound on Function instances; caps what a burst of traffic can cost.')
param maximumInstanceCount int = 10
@description('Function app name; becomes <name>.azurewebsites.net, so it must be globally unique.')
param appName string = 'homesight-api'
@description('Public base URL of the API, written into the served OpenAPI spec. Change when a custom domain is added.')
param publicBaseUrl string = 'https://${appName}.azurewebsites.net'
@description('The live read-model database; flipped by tools/cloud/provision.py flip.')
@allowed([ 'homesight_blue', 'homesight_green' ])
param activeDatabase string

var suffix = uniqueString(resourceGroup().id)
var deploymentContainer = 'app-package'

// ---------- logs (pay per GB ingested; capped per day) ----------

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${prefix}-logs-${suffix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: json('0.1') }
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${prefix}-insights-${suffix}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

// ---------- database ----------

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: '${prefix}-cosmos-${suffix}'
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    capabilities: [ { name: 'EnableServerless' } ]
    locations: [ { locationName: location, failoverPriority: 0, isZoneRedundant: false } ]
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    disableLocalAuth: true
    minimalTlsVersion: 'Tls12'
  }
}

// Built-in Cosmos data-plane roles
var cosmosDataReader = '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000001'
var cosmosDataContributor = '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'

resource loaderCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos
  name: guid(cosmos.id, loaderPrincipalId, 'contributor')
  properties: {
    roleDefinitionId: cosmosDataContributor
    principalId: loaderPrincipalId
    scope: cosmos.id
  }
}

// ---------- function app ----------

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: take('${prefix}st${suffix}', 24)
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    minimumTlsVersion: 'TLS1_2'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource packageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: deploymentContainer
}

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: '${prefix}-plan-${suffix}'
  location: location
  kind: 'functionapp'
  sku: { tier: 'FlexConsumption', name: 'FC1' }
  properties: { reserved: true }
}

resource app 'Microsoft.Web/sites@2024-04-01' = {
  name: appName
  location: location
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: storage.name }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
        { name: 'COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
        { name: 'COSMOS_DATABASE', value: activeDatabase }
        { name: 'PUBLIC_BASE_URL', value: publicBaseUrl }
        // no Functions placeholder page at GET /
        { name: 'AzureWebJobsDisableHomepage', value: 'true' }
      ]
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainer}'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: maximumInstanceCount
        instanceMemoryMB: 2048
      }
      runtime: { name: 'python', version: '3.12' }
    }
  }
}

// The Function's identity: owner of its own storage (host state + deployment package), reader of Cosmos.
var storageBlobDataOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'

resource appStorageRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, app.id, storageBlobDataOwner)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataOwner)
    principalId: app.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource appCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-11-15' = {
  parent: cosmos
  name: guid(cosmos.id, app.id, 'reader')
  properties: {
    roleDefinitionId: cosmosDataReader
    principalId: app.identity.principalId
    scope: cosmos.id
  }
}

output functionAppName string = app.name
output apiBaseUrl string = publicBaseUrl
output cosmosEndpoint string = cosmos.properties.documentEndpoint
output activeDatabase string = activeDatabase
