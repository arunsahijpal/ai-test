<?php
namespace Drupal\product_custom\Controller;

use Drupal\Core\Controller\ControllerBase;
use Symfony\Component\HttpFoundation\Request;
use Drupal\node\Entity\Node;
use Drupal\file\Entity\File;

/**
 * Class ProductCustomController.
 */
class ProductCustomController extends ControllerBase {

  /**
   * Thank you page.
   *
   * @param \Symfony\Component\HttpFoundation\Request $request
   *   The request object.
   *
   * @return array
   *   Render array for the thank you page.
   */
  public function thankYou(Request $request) {
    // Retrieve query parameters.
    $product_id = $request->query->get('product_id');
    $user_name = $request->query->get('user_name');

    // Load the product node.
    $product_node = Node::load($product_id);
    
    if ($product_node) {
      $item_name = $product_node->getTitle();
      $item_image = $product_node->get('field_images')->entity;
      
      // Get the image URL using file_create_url().
      $item_image_url = $item_image ? \Drupal::service('file_url_generator')->generateAbsoluteString($item_image->getFileUri()) : '';

      // Invalidate cache for the thank-you page.
      \Drupal::service('cache_tags.invalidator')->invalidateTags(['node:' . $product_id]);

      // Create the thank you page content.
      return [
        '#theme' => 'item_thank_you_page',
        '#item_name' => $item_name,
        '#item_image' => $item_image_url,
        '#user_name' => $user_name,
        '#quantity' => 1,  // You can adjust this based on your needs.
      ];
    }
    
    // Fallback message if product not found.
    return [
      '#markup' => $this->t('Thank you for your purchase!'),
    ];
  }
}
