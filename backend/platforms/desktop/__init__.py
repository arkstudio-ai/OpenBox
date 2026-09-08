"""Cloud-desktop browser login state (授权中心 · 云电脑登录态).

The person logs into a site inside the one headed Chrome on their cloud
desktop; the cookies stay in that profile until the site drops them. This
package records which sites are logged in, probes them without navigating,
and lets the page push a login screen to the desktop or clear a site's cookies.
"""
