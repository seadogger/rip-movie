#!/bin/sh
# ansible-vault password provider — reads the vault password from the macOS login
# Keychain so it's never stored on disk in this repo.
#
# Protects the committed, ENCRYPTED backup of the gitignored secrets file:
#   config/secrets.vault.env  <- encrypted copy of config/secrets.env
#                                (TMDB + Jellyfin API keys, Nextcloud user/pass)
#
# The working config/secrets.env stays plaintext + gitignored and is used normally;
# secrets.vault.env exists purely as a recoverable offsite backup.
#
# One-time Keychain setup (already done):
#   security add-generic-password -U -a "$USER" -s "ansible-vault:rip-movie" -w "<password>"
#
# View a backup:
#   ansible-vault view    --vault-password-file config/vault-pass-keychain.sh config/secrets.vault.env
# Refresh after editing the plaintext file:
#   ansible-vault encrypt --vault-password-file config/vault-pass-keychain.sh \
#                         --output config/secrets.vault.env config/secrets.env
# Restore on a new machine (after re-adding the Keychain item from your Passwords app):
#   ansible-vault view    --vault-password-file config/vault-pass-keychain.sh \
#                         config/secrets.vault.env > config/secrets.env
#
# DISASTER RECOVERY: this password lives ONLY in your Keychain. The encrypted backup on
# GitHub is unrecoverable without it — also keep it in your Passwords app / password manager.
security find-generic-password -a "$USER" -s "ansible-vault:rip-movie" -w
