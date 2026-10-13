
echo "--- Installed versions"
dpkg-query -W -f='${binary:Package}\t${Version}\n' \
  ovmf ovmf-amdsev ovmf-generic ovmf-inteltdx

echo "--- Held OVMF packages"
apt-mark showhold | grep -E '^ovmf($|-)'



apt-get install -y --allow-downgrades --no-remove \
  ovmf=2025.11-3ubuntu7 \
  ovmf-amdsev=2025.11-3ubuntu7 \
  ovmf-generic=2025.11-3ubuntu7 \
  ovmf-inteltdx=2025.11-3ubuntu7

apt-mark hold ovmf ovmf-amdsev ovmf-generic ovmf-inteltdx

echo "--- Installed versions"
dpkg-query -W ovmf ovmf-amdsev ovmf-generic ovmf-inteltdx

echo "--- Held packages"
apt-mark showhold
