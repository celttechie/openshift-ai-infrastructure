# ==============================================================================
# Cisco Intersight Target - Provider & Organization
# ==============================================================================

provider "intersight" {
  apikey    = var.intersight_api_key_id
  secretkey = fileexists(var.intersight_secret_key_file) ? file(var.intersight_secret_key_file) : "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0dummy...\n-----END RSA PRIVATE KEY-----"
  endpoint  = var.intersight_endpoint
}

data "intersight_organization_organization" "org" {
  name = var.organization_name
}
