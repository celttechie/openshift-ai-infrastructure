# ==============================================================================
# Cisco Intersight Target - Variables
# Declaratively models Cisco UCS AI Server Profiles for Kubernetes / OpenShift
# ==============================================================================

variable "intersight_api_key_id" {
  type        = string
  description = "Cisco Intersight API Key ID (from Cisco DevNet Sandbox or Intersight account)"
  default     = "dummy_api_key_id"
}

variable "intersight_secret_key_file" {
  type        = string
  description = "Path to the RSA private key (.pem) associated with the Intersight API Key"
  default     = "./secret_key.pem"
}

variable "intersight_endpoint" {
  type        = string
  description = "Cisco Intersight API endpoint URL"
  default     = "https://intersight.com"
}

variable "organization_name" {
  type        = string
  description = "Intersight Organization to place policies and server profiles in"
  default     = "default"
}

variable "cluster_name" {
  type        = string
  description = "Target AI cluster identifier"
  default     = "sno-ai-ucs"
}
