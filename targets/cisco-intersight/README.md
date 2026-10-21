# Cisco Intersight Target: UCS AI Server Profiles as Code

This module allows anyone in the community to declaratively model and provision **Cisco UCS AI Server Profiles** using OpenTofu / Terraform against the **free Cisco DevNet Intersight Sandbox** or a production Cisco Intersight account.

---

## 1. Why Cisco Intersight for AI Infrastructure?

In enterprise environments (such as Cisco's AI Infrastructure platforms), physical compute hardware (Cisco UCS X-Series blades, C-Series rack servers) is never manually configured via physical console or BIOS prompts.

Instead, Cisco Intersight acts as a software-defined control plane that applies:
- **Server Profiles**: Abstract identity (UUID, serial, target chassis/slot).
- **BIOS Policies**: Disables power-saving C-States, optimizes memory refresh rates, enables SR-IOV and Intel Virtualization Technology (VT-d).
- **GPU & PCIe Policies**: Directs PCIe Gen5 lanes to high-bandwidth NVIDIA accelerators (A100, H100, L40S).
- **vNIC Policies**: Emulates RoCEv2 (RDMA over Converged Ethernet) fabrics for ultra-low latency inter-GPU communication across clusters.

---

## 2. Quickstart: Getting Free Access to Cisco DevNet Intersight

1. Visit [Cisco DevNet Intersight Sandbox](https://developer.cisco.com/site/intersight/).
2. Log in with a free Cisco DevNet account.
3. In the Intersight dashboard, navigate to **Settings (Gear Icon) -> API Keys**.
4. Click **Generate API Key**:
   - Save the **API Key ID**.
   - Download the **Secret Key (.pem)** and save it as `secret_key.pem` in this directory (it is git-ignored).
5. Copy `terraform.tfvars.example` to `terraform.tfvars` and fill in your API key ID.
6. Run:
   ```bash
   tofu init
   tofu plan
   tofu apply
   ```

---

## 3. Module Outputs & Result

Upon `tofu apply`, Intersight creates:
- A standardized **AI Compute BIOS Policy** with deterministic, enterprise-grade hardware flags.
- A **UCS AI Server Profile** ready to bind to bare-metal compute blades running OpenShift or Kubernetes.
