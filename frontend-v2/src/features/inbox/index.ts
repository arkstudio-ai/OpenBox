// Public surface of the message centre: the page, the public topic page, the
// drawer badge query and the socket bridge the layout mounts once.
export { InboxPage } from "./components/InboxPage"
export { TopicPage } from "./components/TopicPage"
export { useInboxUnread, useInboxLiveEvents } from "./api"
export { planInboxLink, useOpenInboxLink } from "./lib/resolveLink"
