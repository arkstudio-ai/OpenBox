import Flutter
import UIKit

class SceneDelegate: FlutterSceneDelegate {
  override func scene(_ scene: UIScene, willConnectTo session: UISceneSession, options connectionOptions: UIScene.ConnectionOptions) {
    super.scene(scene, willConnectTo: session, options: connectionOptions)
    if let response = connectionOptions.notificationResponse,
       let app = UIApplication.shared.delegate as? AppDelegate {
      app.notifications.opened(response.notification.request.content.userInfo)
    }
  }

  override func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
    if let url = URLContexts.first?.url,
       let appDelegate = UIApplication.shared.delegate as? AppDelegate,
       appDelegate.handleAlipay(url: url) {
      return
    }
    super.scene(scene, openURLContexts: URLContexts)
  }
}
