ui = false

api_addr     = "http://127.0.0.1:8200"
cluster_addr = "http://127.0.0.1:8201"

storage "raft" {
  path    = "/openbao/file/raft"
  node_id = "infra-manager"
}

listener "tcp" {
  address         = "127.0.0.1:8200"
  cluster_address = "127.0.0.1:8201"
  tls_disable     = true
}

listener "tcp" {
  address       = "0.0.0.0:8202"
  tls_disable   = false
  tls_cert_file = "/openbao/tls/server.crt"
  tls_key_file  = "/openbao/tls/server.key"
}
