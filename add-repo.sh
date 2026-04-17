#!/bin/sh

REPO_NAME="termux-mcp"
REPO_URL="https://termux-mcp.pages.dev"
echo "Adding $REPO_NAME repository to Termux..."
mkdir -p $PREFIX/etc/apt/sources.list.d
cat > $PREFIX/etc/apt/sources.list.d/$REPO_NAME.list << EOF
deb [trusted=yes] $REPO_URL ./
EOF

echo "📦 Updating package lists..."
apt update

echo ""
echo "✅ Repository added successfully!"
echo ""
echo "You can now install Termux MCP with:"
echo "   pkg install termux-mcp"
echo ""
