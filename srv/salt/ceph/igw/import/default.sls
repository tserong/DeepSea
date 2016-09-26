

lrbd:
  pkg.installed:
    - pkgs:
      - lrbd

/tmp/lrbd.conf:
  file.managed:
    - source: 
      - salt://ceph/igw/files/lrbd.conf
    - user: root
    - group: root
    - mode: 600

configure:
  cmd.run:
    - name: "lrbd -f /tmp/lrbd.conf"
    - shell: /bin/bash
    - require:
      - file: /tmp/lrbd.conf
