using UnityEngine;
#if ENABLE_INPUT_SYSTEM
using UnityEngine.InputSystem;
#endif

/// <summary>
/// Free camera controller for navigating the scene during Play mode.
/// Controls:
/// - WASD: Move forward/backward/left/right
/// - Q/E: Move down/up
/// - Right Mouse Button + Mouse: Look around
/// - Shift: Move faster
/// - Scroll Wheel: Adjust movement speed
/// </summary>
public class FreeCameraController : MonoBehaviour
{
    [Header("Movement Settings")]
    public float moveSpeed = 10f;
    public float fastMoveMultiplier = 3f;
    public float scrollSpeedAdjustment = 5f;

    [Header("Look Settings")]
    public float lookSensitivity = 1.2f;
    public float maxLookAngle = 90f;

    [Header("Dynamic Focus")]
    public Transform startPoint;
    public Transform endPoint;
    public float autoZoomPadding = 1.2f;

    [Header("Overhead View")]
    public float overheadHeight = 250f;
    public Vector2 overheadCenterPosition = new Vector2(-1079.99f, -142.263f);
    public float overheadPitch = 89f;
    public float overheadYaw = -360f;
    public bool isOverheadView = false;
    public float overheadZoomSpeed = 120f;
    public float overheadMinHeight = 20f;
    public float overheadMaxHeight = 2000f;
    public float overheadOrthoSize = 165f;
    public float overheadMinOrthoSize = 20f;
    public float overheadMaxOrthoSize = 2000f;
    public float overheadDragPanSpeed = 0.15f;

    [Header("Free View Zoom")]
    public float freeViewZoomSpeed = 120f;

    private float rotationX = 0f;
    private float rotationY = 0f;
    private Vector3 savedFreeViewPosition;
    private Quaternion savedFreeViewRotation;
    private Camera cachedCamera;
    private bool savedOrthographic;
    private float savedOrthographicSize;

    void Start()
    {
        // Initialize rotation from current camera orientation
        Vector3 currentRotation = transform.eulerAngles;
        rotationX = currentRotation.y;
        rotationY = currentRotation.x;
        
        // Normalize the Y rotation to avoid camera flip
        if (rotationY > 180f)
            rotationY -= 360f;

        savedFreeViewPosition = transform.position;
        savedFreeViewRotation = transform.rotation;
        cachedCamera = GetComponent<Camera>();
        if (cachedCamera != null)
        {
            savedOrthographic = cachedCamera.orthographic;
            savedOrthographicSize = cachedCamera.orthographicSize;
        }
    }

    void Update()
    {
        HandleMovement();
        HandleRotation();
        HandleSpeedAdjustment();
    }

    void HandleMovement()
    {
        float currentSpeed = moveSpeed;
        
        // Hold Shift to move faster
        if (IsShiftPressed())
        {
            currentSpeed *= fastMoveMultiplier;
        }

        Vector3 moveDirection = Vector3.zero;

        // WASD movement
        if (isOverheadView)
        {
            if (IsMoveForwardPressed())
                moveDirection += Vector3.forward;
            if (IsMoveBackwardPressed())
                moveDirection += Vector3.back;
            if (IsMoveLeftPressed())
                moveDirection += Vector3.left;
            if (IsMoveRightPressed())
                moveDirection += Vector3.right;
        }
        else
        {
            if (IsMoveForwardPressed())
                moveDirection += transform.forward;
            if (IsMoveBackwardPressed())
                moveDirection -= transform.forward;
            if (IsMoveLeftPressed())
                moveDirection -= transform.right;
            if (IsMoveRightPressed())
                moveDirection += transform.right;
        }

        // Q/E for vertical movement
        if (IsMoveUpPressed())
            moveDirection += Vector3.up;
        if (IsMoveDownPressed())
            moveDirection -= Vector3.up;

        // Use unscaled time so camera navigation still works when gameplay is paused (timeScale = 0)
        transform.position += moveDirection.normalized * currentSpeed * Time.unscaledDeltaTime;
    }

    void HandleRotation()
    {
        if (isOverheadView)
        {
            Cursor.lockState = CursorLockMode.None;
            Cursor.visible = true;

            // Left-click drag pans the overhead view on world XZ.
            if (IsPanPressed())
            {
                Vector2 dragDelta = GetLookDelta();
                Vector3 pan = new Vector3(-dragDelta.x, 0f, -dragDelta.y) * overheadDragPanSpeed;
                transform.position += pan;
            }
            return;
        }

        // Only rotate when right mouse button is held
        if (IsLookPressed())
        {
            // Hide cursor while looking
            Cursor.lockState = CursorLockMode.Locked;
            Cursor.visible = false;

            // Get mouse input
            Vector2 lookDelta = GetLookDelta() * lookSensitivity;
            float mouseX = lookDelta.x;
            float mouseY = lookDelta.y;

            // Update rotation values
            rotationX += mouseX;
            rotationY -= mouseY;

            // Clamp vertical rotation to prevent flipping
            rotationY = Mathf.Clamp(rotationY, -maxLookAngle, maxLookAngle);

            // Apply rotation
            transform.rotation = Quaternion.Euler(rotationY, rotationX, 0f);
        }
        else
        {
            // Show cursor when not looking
            Cursor.lockState = CursorLockMode.None;
            Cursor.visible = true;
        }
    }

    public void ToggleOverheadView()
    {
        SetOverheadView(!isOverheadView);
    }

    public void SetOverheadView(bool enabled)
    {
        if (enabled == isOverheadView)
            return;

        if (enabled)
        {
            savedFreeViewPosition = transform.position;
            savedFreeViewRotation = transform.rotation;
            if (cachedCamera != null)
            {
                savedOrthographic = cachedCamera.orthographic;
                savedOrthographicSize = cachedCamera.orthographicSize;
            }

            Vector3 pos = transform.position;
            
            // DYNAMIC POSITIONING PINPOINT
            // If start/end points are assigned, center between them. 
            // Otherwise, fall back to hardcoded overheadCenterPosition.
            if (startPoint != null && endPoint != null)
            {
                Vector3 midpoint = (startPoint.position + endPoint.position) / 2f;
                pos.x = midpoint.x;
                pos.z = midpoint.z;

                // Adjust zoom to fit both points
                float distance = Vector3.Distance(startPoint.position, endPoint.position);
                overheadOrthoSize = (distance / 2f) * autoZoomPadding;
            }
            else
            {
                pos.x = overheadCenterPosition.x;
                pos.z = overheadCenterPosition.y;
            }

            pos.y = overheadHeight;
            transform.position = pos;
            transform.rotation = Quaternion.Euler(overheadPitch, overheadYaw, 0f);
            
            if (cachedCamera != null)
            {
                cachedCamera.orthographic = true;
                cachedCamera.orthographicSize = Mathf.Clamp(overheadOrthoSize, overheadMinOrthoSize, overheadMaxOrthoSize);
            }
            rotationX = overheadYaw;
            rotationY = overheadPitch;
            isOverheadView = true;
        }
        else
        {
            transform.position = savedFreeViewPosition;
            transform.rotation = savedFreeViewRotation;
            if (cachedCamera != null)
            {
                cachedCamera.orthographic = savedOrthographic;
                cachedCamera.orthographicSize = savedOrthographicSize;
            }
            Vector3 euler = transform.eulerAngles;
            rotationX = euler.y;
            rotationY = euler.x;
            if (rotationY > 180f)
                rotationY -= 360f;
            isOverheadView = false;
        }
    }

    void HandleSpeedAdjustment()
    {
        // In overhead mode, scroll controls zoom (camera height).
        if (isOverheadView)
        {
            float scrollZoom = GetScrollDelta();
            if (scrollZoom != 0f)
            {
                if (cachedCamera != null)
                {
                    overheadOrthoSize -= scrollZoom * overheadZoomSpeed * scrollSpeedAdjustment;
                    overheadOrthoSize = Mathf.Clamp(overheadOrthoSize, overheadMinOrthoSize, overheadMaxOrthoSize);
                    cachedCamera.orthographicSize = overheadOrthoSize;
                }
                else
                {
                    Vector3 pos = transform.position;
                    // Fallback if camera component is missing.
                    pos.y -= scrollZoom * overheadZoomSpeed * scrollSpeedAdjustment;
                    pos.y = Mathf.Clamp(pos.y, overheadMinHeight, overheadMaxHeight);
                    transform.position = pos;
                }
            }
            return;
        }

        // In free view, scroll zooms along camera forward direction.
        float scroll = GetScrollDelta();
        if (scroll != 0f)
        {
            transform.position += transform.forward * (scroll * freeViewZoomSpeed * scrollSpeedAdjustment);
        }
    }

    private bool IsShiftPressed()
    {
#if ENABLE_INPUT_SYSTEM
        Keyboard kb = Keyboard.current;
        if (kb != null)
        {
            return kb.leftShiftKey.isPressed || kb.rightShiftKey.isPressed;
        }
#endif
#if ENABLE_LEGACY_INPUT_MANAGER
        return Input.GetKey(KeyCode.LeftShift) || Input.GetKey(KeyCode.RightShift);
#else
        return false;
#endif
    }

    private bool IsMoveForwardPressed()
    {
#if ENABLE_INPUT_SYSTEM
        Keyboard kb = Keyboard.current;
        if (kb != null) return kb.wKey.isPressed;
#endif
#if ENABLE_LEGACY_INPUT_MANAGER
        return Input.GetKey(KeyCode.W);
#else
        return false;
#endif
    }

    private bool IsMoveBackwardPressed()
    {
#if ENABLE_INPUT_SYSTEM
        Keyboard kb = Keyboard.current;
        if (kb != null) return kb.sKey.isPressed;
#endif
#if ENABLE_LEGACY_INPUT_MANAGER
        return Input.GetKey(KeyCode.S);
#else
        return false;
#endif
    }

    private bool IsMoveLeftPressed()
    {
#if ENABLE_INPUT_SYSTEM
        Keyboard kb = Keyboard.current;
        if (kb != null) return kb.aKey.isPressed;
#endif
#if ENABLE_LEGACY_INPUT_MANAGER
        return Input.GetKey(KeyCode.A);
#else
        return false;
#endif
    }

    private bool IsMoveRightPressed()
    {
#if ENABLE_INPUT_SYSTEM
        Keyboard kb = Keyboard.current;
        if (kb != null) return kb.dKey.isPressed;
#endif
#if ENABLE_LEGACY_INPUT_MANAGER
        return Input.GetKey(KeyCode.D);
#else
        return false;
#endif
    }

    private bool IsMoveUpPressed()
    {
#if ENABLE_INPUT_SYSTEM
        Keyboard kb = Keyboard.current;
        if (kb != null) return kb.eKey.isPressed;
#endif
#if ENABLE_LEGACY_INPUT_MANAGER
        return Input.GetKey(KeyCode.E);
#else
        return false;
#endif
    }

    private bool IsMoveDownPressed()
    {
#if ENABLE_INPUT_SYSTEM
        Keyboard kb = Keyboard.current;
        if (kb != null) return kb.qKey.isPressed;
#endif
#if ENABLE_LEGACY_INPUT_MANAGER
        return Input.GetKey(KeyCode.Q);
#else
        return false;
#endif
    }

    private bool IsLookPressed()
    {
#if ENABLE_INPUT_SYSTEM
        Mouse mouse = Mouse.current;
        if (mouse != null) return mouse.rightButton.isPressed;
#endif
#if ENABLE_LEGACY_INPUT_MANAGER
        return Input.GetMouseButton(1);
#else
        return false;
#endif
    }

    private bool IsPanPressed()
    {
#if ENABLE_INPUT_SYSTEM
        Mouse mouse = Mouse.current;
        if (mouse != null) return mouse.leftButton.isPressed;
#endif
#if ENABLE_LEGACY_INPUT_MANAGER
        return Input.GetMouseButton(0);
#else
        return false;
#endif
    }

    private Vector2 GetLookDelta()
    {
#if ENABLE_INPUT_SYSTEM
        Mouse mouse = Mouse.current;
        if (mouse != null) return mouse.delta.ReadValue();
#endif
#if ENABLE_LEGACY_INPUT_MANAGER
        return new Vector2(Input.GetAxis("Mouse X"), Input.GetAxis("Mouse Y"));
#else
        return Vector2.zero;
#endif
    }

    private float GetScrollDelta()
    {
#if ENABLE_INPUT_SYSTEM
        Mouse mouse = Mouse.current;
        if (mouse != null) return mouse.scroll.ReadValue().y / 120f;
#endif
#if ENABLE_LEGACY_INPUT_MANAGER
        return Input.GetAxis("Mouse ScrollWheel") * 10f;
#else
        return 0f;
#endif
    }
}
