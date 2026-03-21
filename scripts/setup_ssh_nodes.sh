#!/usr/bin/env bash
set -e

# Change to project root
cd "$(dirname "$0")/.."

DATA_DIR="data"
KEY_DIR="$DATA_DIR/ssh_keys"

echo "Setting up SSH environment..."
mkdir -p "$KEY_DIR"

# Ensure correct permissions for data dir (important for CA generation/reading)
chmod 700 "$KEY_DIR"

# Generate expected CA key
## generate ca, ca.pub 
## ca.pub copy to /etc/ssh/ca.pub in node
## ca used to sign client_ca.pub
if [ ! -f "$KEY_DIR/ca" ]; then
    ssh-keygen -t ed25519 -f "$KEY_DIR/ca" -C "CA" -N ""
fi

# Generate expected Client Key for CA signing
if [ ! -f "$KEY_DIR/client_ca" ]; then
    # generate client key 
    ## client_ca is client's private key
    ssh-keygen -t ed25519 -f "$KEY_DIR/client_ca" -C "Client-CA" -N ""
    # Sign it with CA, generate client_ca-cert.pub (certificate using ca sign with client_ca)
    ## -s <ca_key> use ca to sign client key
    ## -n <user> set user
    ## -V +52w set valid time to 52 weeks
    ## -I <cert_id> set cert id
    ssh-keygen -s "$KEY_DIR/ca" -n root -V +52w -I "client_cert" "$KEY_DIR/client_ca.pub"
fi

# Generate normal standalone Client Key
if [ ! -f "$KEY_DIR/client_key" ]; then
    ssh-keygen -t ed25519 -f "$KEY_DIR/client_key" -C "Client-Key" -N ""
fi

# Generate the allow-commands-admin.json
cat << 'EOF' > "$DATA_DIR/allow-commands-admin.json"
{
  "name": "admin",
  "allow_commands": [
    {
      "command_name": "reboot",
      "description": "reboot",
      "disconnects_ssh": true,
      "killable": false,
      "pipeline": [{"command": ["reboot"]}],
      "arguments": []
    },
    {
      "command_name": "sleep",
      "description": "sleep",
      "disconnects_ssh": false,
      "killable": true,
      "pipeline": [{"command": ["sleep", "{time}"]}],
      "arguments": [{"name": "time", "type": "int", "validation_regex": "^[0-9]+$"}]
    },
    {
      "command_name": "list_file",
      "description": "list file",
      "disconnects_ssh": false,
      "killable": true,
      "pipeline": [
        {"command": ["ls", "-al"]},
        {"command": ["grep", "{key_word}"]}
      ],
      "arguments": [{"name": "key_word", "type": "string", "validation_regex": "^[a-zA-Z0-9._-]+$"}]
    },
    {
      "command_name": "whoami",
      "description": "whoami",
      "disconnects_ssh": false,
      "killable": true,
      "pipeline": [
        {
          "command": [
            "whoami"
          ]
        }
      ],
      "arguments": []
    }
  ]
}
EOF

# Generate SSH configs

# Base64 encode the keys (MacOS/Linux compatible removing newlines)
B64_CA_KEY=$(cat "$KEY_DIR/client_ca" | base64 | tr -d '\n')
B64_CA_CERT=$(cat "$KEY_DIR/client_ca-cert.pub" | base64 | tr -d '\n')

cat << EOF > "$DATA_DIR/SSH-cluster1.json"
{
  "host": "localhost",
  "port": 2222,
  "username": "root",
  "auth_method": "ca",
  "key_base64": "$B64_CA_KEY",
  "cert_base64": "$B64_CA_CERT"
}
EOF

DEFAULT_B64_KEY=$(cat "$KEY_DIR/client_key" | base64 | tr -d '\n')

cat << EOF > "$DATA_DIR/SSH-default.json"
{
  "host": "localhost",
  "port": 2223,
  "username": "root",
  "auth_method": "key",
  "key_base64": "$DEFAULT_B64_KEY"
}
EOF

# Down then Up the containers
docker compose -f docker-compose-nodes.yml down -v
docker compose -f docker-compose-nodes.yml up -d --build

echo "Done! Nodes are running on localhost:2222 and localhost:2223"
