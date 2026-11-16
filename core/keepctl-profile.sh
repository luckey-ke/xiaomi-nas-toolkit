# MINAS keepctl: put the management tool on PATH for interactive logins
case ":$PATH:" in
  *:/data/minas-ssh/keep-plugins:*) ;;
  *) PATH="$PATH:/data/minas-ssh/keep-plugins"; export PATH ;;
esac
