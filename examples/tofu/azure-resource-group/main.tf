# An example reviewed module for the cloud executor: one Azure resource group with tags.
# The executor's environment carries the credentials (ARM_CLIENT_ID, ARM_CLIENT_SECRET, ARM_TENANT_ID,
# ARM_SUBSCRIPTION_ID); add a backend block (for example azurerm) to keep state off the executor's disk.
terraform {
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 4.0" }
  }
}

provider "azurerm" {
  features {}
}

variable "name" { type = string }
variable "location" { type = string }
variable "owner" { type = string }

resource "azurerm_resource_group" "this" {
  name     = var.name
  location = var.location
  tags     = { owner = var.owner, managed_by = "rezztor-orchestrator" }
}
