FROM buildbot/buildbot-master:v2.4.1

ADD config_dir/* /var/lib/buildbot/

RUN ln -sf /var/lib/buildbot/master.cfg-example /var/lib/buildbot/master.cfg
