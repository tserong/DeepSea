
{% set master = salt['master.minion']() %}

{% if salt.saltutil.runner('select.minions', cluster='ceph', roles='ganesha') or salt.saltutil.runner('select.minions', cluster='ceph', ganesha_configurations='*') %}

ganesha auth:
  salt.state:
    - tgt: {{ master }}
    - tgt_type: compound
    - sls: ceph.ganesha.auth

ganesha config:
  salt.state:
    - tgt: {{ master }}
    - tgt_type: compound
    - sls: ceph.ganesha.config
    - failhard: True

{% for role in salt['pillar.get']('ganesha_configurations', [ 'ganesha' ]) %}
start {{ role }}::
  salt.state:
    - tgt: "I@roles:{{ role }} and I@cluster:ceph"
    - tgt_type: compound
    - sls: ceph.ganesha

{% endfor %}

{% endif %}
