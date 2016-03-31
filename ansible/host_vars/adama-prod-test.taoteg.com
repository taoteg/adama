---

# VARS
# Adama Development VM (on rodeo)
ansible_ssh_host: 129.114.6.110
ansible_ssh_user: rodeo
ansible_ssh_private_key_file: ~/.ssh/jgentle-tacc-rodeo.pem
host_name: adama-prod-test.taoteg.com
host_queue_ip: 172.17.0.1
host_queue_port: 5672
host_store_port: 6379
host_store_value: localhost
host_bind_addr: "0.0.0.0:80"
api_prefix_path: /community/v0.3
api_public_key_val: MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCUp/oV1vWc8/TkQSiAvTousMzOM4asB2iltr2QKozni5aVFu818MpOLZIr8LMnTzWllJvvaA5RAAdpbECb+48FjbBe0hseUdN5HpwvnH/DW8ZccGvk53I6Orq7hLCv1ZHtuOCokghz/ATrhyPq+QktMfXnRS4HrKGJTzxaCcU7OQIDAQAB
api_token_value: a6233e2f48c1b4332e3d42f957c85446
notification_recipients: dlaraport-notifications@jcvi.org, jgentle@tacc.utexas.edu
autobackup_scp_user: araport@corral.tacc.utexas.edu
autobackup_scp_path: /corral-tacc/tacc/bio/araport/users/araport/adama

config:
  -
    option: python_instances
    section: workers
    value: 2
  -
    option: javascript_instances
    section: workers
    value: 1
  -
    option: ruby_instances
    section: workers
    value: 1
  -
    option: command
    section: docker
    value: docker
  -
    option: host
    section: docker
    value: ""
  -
    option: host
    section: queue
    value: "{{host_queue_ip}}"
    #value: "{{ hostvars['adama-dev-1']['ansible_docker0']['ipv4']['address'] }}"
  -
    option: port
    section: queue
    value: "{{host_queue_port}}"
  -
    option: host
    section: store
    value: "{{host_store_value}}"
  -
    option: port
    section: store
    value: "{{host_store_port}}"
  -
    option: url
    section: server
    value: http://"{{host_name}}"
    #value: http://"{{ hostvars['adama-dev-1']['ansible_ssh_host'] }}"
  -
    option: bind
    section: server
    value: "{{host_bind_addr}}"
  -
    option: prefix
    section: server
    value: "{{api_prefix_path}}"
  -
    option: api_prefix
    section: server
    value: "{{api_prefix_path}}"
    #value: /community/v"{{ lookup('file', '../adama/VERSION') }}"
  -
    option: api_docs
    section: server
    value: https://api.araport.org"{{api_prefix_path}}"
  -
    option: api_url
    section: server
    value: http://"{{host_name}}"
  -
    option: swagger_ui
    section: server
    value: http://"{{host_name}}"/swagger-ui
    #value: http://"{{ hostvars['adama-dev-1']['ansible_ssh_host'] }}":8080/swagger-ui
  -
    option: access_control
    section: server
    value: none
  -
    option: tenant_name
    section: server
    value: araport-org
  -
    option: apim_public_key
    section: server
    value: "{{ api_public_key_val }}"

monitor:
  -
    option: smarthost
    section: mail
    value: smtp-relay.gmail.com
  -
    option: port
    section: mail
    value: 465
  -
    option: user
    section: mail
    value: AraportUser@gmail.com
  -
    option: password
    section: mail
    value: NONSENSE
  -
    option: notify
    section: monitor
    value: "{{notification_recipients}}"
  -
    option: url
    section: api
    value: http://"{{host_name}}""{{api_prefix_path}}"
  -
    option: token
    section: api
    value: "{{api_token_value}}"

autobackup:
  -
    option: login
    section: scp
    value: "{{autobackup_scp_user}}"
  -
    option: path
    section: scp
    value: "{{autobackup_scp_path}}"

nginx_server_name: "{{host_name}}"
self_sign: true
