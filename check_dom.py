from DrissionPage import ChromiumPage

def check_dom():
    print("正在连接浏览器...")
    try:
        page = ChromiumPage()
        
        print("\n--- 验证 chat-message ---")
        items = page.eles('css:[data-e2e="chat-message"]')
        print(f"找到 {len(items)} 条消息。")
        
        if items:
            last_item = items[-1]
            print(f"最后一条消息 HTML:\n{last_item.html[:300]}")
            print(f"最后一条消息 Text: {last_item.text}")
            
            # 尝试在这个容器内找用户名
            user = last_item.ele('css:[data-e2e="message-owner-name"]')
            if user:
                print(f"提取用户名成功: {user.text}")
            else:
                print("提取用户名失败")
                
            # 尝试找内容
            # 根据经验，内容可能是除了用户名之外的文本，或者在特定的 span 里
            # 我们打印所有子元素的 data-e2e
            children = last_item.eles('css:*')
            print("子元素 data-e2e:", [c.attr('data-e2e') for c in children if c.attr('data-e2e')])

    except Exception as e:
        print(f"发生错误: {e}")

if __name__ == "__main__":
    check_dom()
