// A complete offline replacement, sharing the installed app identity and data path.
const { build } = require('./package.json')

module.exports = {
  ...build,
  nsis: {
    ...build.nsis,
    artifactName: 'MailGroup-${version}-${arch}-update.${ext}',
    allowToChangeInstallationDirectory: false,
    include: 'installer-update.nsh',
  },
}
