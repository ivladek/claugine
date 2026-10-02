#!/bin/bash
#
# sample script to install HPE SUM on Rocky 10
# run locally on SUM host
# v0.0.1 #2026-10-02

ILO_IP=1.2.3.4
ILO_USER=Administrator
SUM_HOSTNAME=shpesum.acme.com
SUM_URL=http://localrepo.acme.com/zakroma/hpe
G11_ISO=P97792_001_gen11spp-2026.07.00.00-Gen11SPP2026070000.2026_0806.15.iso
G12_ISO=P97793_001_gen12spp-2026.07.00.00-Gen12SPP2026070000.2026_0806.32.iso

# hostname is a flag of first boot
if [[ "$(hostnamectl hostname)" != "${SUM_HOSTNAME}" ]]
then
  sudo hostnamectl set-hostname ${SUM_HOSTNAME}
  sudo hostnamectl set-hostname "" --pretty
  sudo dnf upgrade --refresh -y
  sudo reboot
fi

# setup repo and install SUM and required libcrypt.so.1 to avoid errors with ILO7
sudo tee /etc/yum.repos.d/sum.repo &>/dev/null <<'EOF'
[sum]
name=HPE Smart Update Manager
baseurl=https://downloads.linux.hpe.com/repo/sum/rhel/10/x86_64/current/
enabled=1
gpgcheck=0
EOF

sudo dnf --disablerepo='*' --enablerepo=sum makecache --refresh
sudo dnf --disablerepo='*' --enablerepo=sum list --showduplicates sum
rpm -q sum
sudo dnf -y install sum-12.6.0-9.rhel10 'libcrypt.so.1()(64bit)'

# in case of firewall enabled, open port for SUM service
sudo firewall-cmd --state
sudo firewall-cmd --add-port=63002/tcp
sudo firewall-cmd --permanent --add-port=63002/tcp

# check ILO connection
openssl s_client -connect ${ILO_IP}:443 -showcerts </dev/null 2>/dev/null |
openssl x509 -noout -subject -issuer -dates -fingerprint -sha256
sudo /opt/sum/bin/x64/rest/ilorest --version

# download SPP ISO images and mount them
curl \
  --noproxy '*' \
  --connect-timeout 5 \
  --max-time 20 \
  -u ${ILO_USER} \
  https://${ILO_IP}/redfish/v1/Systems/

mkdir -p sum/g11
mkdir -p sum/g12

curl -o ${HOME}/sum/g11/hpe-sum-g11.iso ${SUM_URL}/${G11_ISO}
curl -o ${HOME}/sum/g12/hpe-sum-g12.iso ${SUM_URL}/${G12_ISO}

sudo mount -o loop,ro ${HOME}/sum/g11/hpe-sum-g11.iso ${HOME}/sum/g11
sudo mount -o loop,ro ${HOME}/sum/g12/hpe-sum-g12.iso ${HOME}/sum/g12

# start SUM service
sudo /opt/sum/bin/smartupdate

# check SUM service status
pgrep -af 'smartupdate|hpsum|sum_service'
sudo ss -lntp

# stop SUM service
#sudo /opt/sum/bin/smartupdate shutdownengine
