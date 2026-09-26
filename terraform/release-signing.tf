# The release-signing key (docs/superpowers/specs/2026-09-25-ota-update-design.md, 6.3).
#
# Every enrolled panel will install, unattended and as root, whatever this
# key signs. The key is therefore in KMS and nowhere else: not on the owner's
# machine, not in a GitHub secret, not in the repository. A secret can be
# read by any job admitted to its environment and copied without a trace; a
# KMS Sign is a CloudTrail event every time, from a caller with a session
# issuer, so a Sign by any other caller CAN be alarmed on. Rotation and
# revocation are one Terraform change, not a hunt for copies.
#
# What is NOT in place yet, said plainly: no alarm reads KMS events today.
# The HockeyTrack repository's terraform/security-alarms.tf has no pattern
# with eventSource kms.amazonaws.com, so a Sign by a stranger, a
# PutKeyPolicy, a CreateGrant, an UpdateAlias or a ScheduleKeyDeletion on
# this key is a CloudTrail record nobody is paged about until that alarm
# lands (design section 9 lists all five as things that must page; it is a
# ticket in that repository, since the alarms live there). Until then the
# detective control on this key is the monthly rule the design gives the
# owner -- the Sign count equals the releases that month -- and the
# imagecheck monitor, which verifies the mirrored manifest against this
# key's public half twice a day and notices a swapped or deleted key by its
# effect, not by the event.
#
# What a compromise costs, plainly: whoever can call Sign on this key AND put
# files where panels fetch them runs code as root on every enrolled panel
# within a day. The key policy narrows Sign to one role, which STS issues
# only to a job in the image-release environment, which only the owner can
# approve, for a tag only an administrator can create. A signed, approved,
# malicious release is not stopped by anything technical here; the approval
# is a person, and this file does not pretend otherwise.

resource "aws_kms_key" "release_signing" {
  description              = "Signs scoreboard release manifests; only the image publisher role may Sign"
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "ECC_NIST_P256"
  # Long enough for a mistaken deletion to be noticed by the imagecheck
  # monitor, which verifies the mirrored manifest against this key twice a
  # day and pages when it cannot.
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.release_signing_key.json
  tags = {
    # The id the manifest carries and the name of the public key's file in
    # device/certs/release-signing/. A rotation creates a second key with the
    # next id; the panel holds both public halves for one release.
    KeyId = "release-2026-1"
  }
}

resource "aws_kms_alias" "release_signing" {
  name          = "alias/scoreboard-release-signing"
  target_key_id = aws_kms_key.release_signing.key_id
}

data "aws_iam_policy_document" "release_signing_key" {
  # Management of the key stays with the account, so a lost role cannot
  # strand it. Be precise about what that principal means: the account's
  # `:root` ARN in a KMS key policy is the "delegate to IAM" idiom, not the
  # root user alone. Any IAM principal whose own policy allows
  # kms:PutKeyPolicy or kms:CreateGrant on this key -- the administrator
  # user, or anyone who is ever given AdministratorAccess -- can rewrite this
  # policy or grant kms:Sign to another principal without touching the root
  # credentials. So the management plane of this key is as wide as IAM
  # administration in the account, and (see the header) none of its events
  # pages yet. No Sign is granted here: even an administrator signs nothing
  # without first changing this policy, which is a CloudTrail record, and
  # after the alarm ticket lands it is a page.
  statement {
    sid = "Administer"
    actions = ["kms:Create*", "kms:Describe*", "kms:Enable*", "kms:List*", "kms:Put*", "kms:Update*",
      "kms:Revoke*", "kms:Disable*", "kms:Get*", "kms:Delete*", "kms:TagResource",
    "kms:UntagResource", "kms:ScheduleKeyDeletion", "kms:CancelKeyDeletion"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
  # Sign: the publisher role only, and only the algorithm the panel checks.
  # The role's own policy grants the same actions on this one key, so both
  # halves of the IAM evaluation name it. GetPublicKey is a separate
  # statement because kms:SigningAlgorithm is absent from that call and a
  # StringEquals on a missing key denies.
  statement {
    sid       = "SignReleases"
    actions   = ["kms:Sign"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.image_publisher.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:SigningAlgorithm"
      values   = ["ECDSA_SHA_256"]
    }
  }
  statement {
    sid       = "PublisherReadsPublicKey"
    actions   = ["kms:GetPublicKey"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.image_publisher.arn]
    }
  }
  # The public half, for the monitor (which verifies the mirrored manifest
  # against KMS rather than the repository, so a swapped repository key does
  # not fool it) and for the owner's one-time export into the repository.
  statement {
    sid       = "ReadPublicKey"
    actions   = ["kms:GetPublicKey", "kms:DescribeKey"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.imagecheck.arn]
    }
  }
}

# The publisher role's side of the grant: Sign and GetPublicKey on this key
# and nothing else. The alias is what the workflow names, so it resolves to
# whichever key is current after a rotation without a workflow change.
data "aws_iam_policy_document" "image_publisher_signing" {
  statement {
    sid       = "SignManifest"
    actions   = ["kms:Sign", "kms:GetPublicKey"]
    resources = [aws_kms_key.release_signing.arn]
  }
}

resource "aws_iam_role_policy" "image_publisher_signing" {
  name   = "sign-release-manifests"
  role   = aws_iam_role.image_publisher.id
  policy = data.aws_iam_policy_document.image_publisher_signing.json
}

data "aws_iam_policy_document" "imagecheck_signing" {
  statement {
    sid       = "ReadSigningKey"
    actions   = ["kms:GetPublicKey", "kms:DescribeKey"]
    resources = [aws_kms_key.release_signing.arn]
  }
}

resource "aws_iam_role_policy" "imagecheck_signing" {
  name   = "read-release-signing-key"
  role   = aws_iam_role.imagecheck.id
  policy = data.aws_iam_policy_document.imagecheck_signing.json
}

output "release_signing_key_arn" {
  value       = aws_kms_key.release_signing.arn
  description = "Export its public half once into device/certs/release-signing/ (see the README there)"
}
